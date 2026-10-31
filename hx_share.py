"""MapleJet files: the v2 folders on the share and on this PC.

Every share access for MapleJet data goes through here. Records and status files are
written whole and swapped in, and read through handles that never block the swap.
Logs are add-only: each entry is one file, written once under its final name and
ended with a completion marker, so a reader never takes an unfinished file.

Printer locks, heartbeats and the poller assignment keep one PC talking to each
printer. The local copy lets a PC send for 24 hours without the share, and log
entries that couldn't reach the share wait in a queue on the PC.

Every share operation runs on an hx-io thread and gives up after share_timeout_s, so
a dead file server never holds anything up. Freshness is judged only from counters
watched on this PC's own monotonic clock, never from file times or another PC's clock.
"""

from __future__ import annotations

import collections
import copy
import dataclasses
import errno
import hashlib
import itertools
import json
import math
import os
import re
import secrets
import shutil
import sys
import tempfile
import threading
import time
import traceback
import zlib
from dataclasses import dataclass
from typing import Callable

import hx_win
import i18n
from i18n import N_
from hx_core import SYSTEM_CLOCK, CancelToken, Clock, EventBus, Msg
from hx_model import (
    FORMAT, LOCK_PURPOSES, LOG_KINDS, TIMINGS, Ack, Calibration, Company, DataUpdate, ExpectedState,
    FileRef, Film, FormatTooNew, HeartbeatInfo, HxData, Identity, Line, LineStatus, LockHeld, LockInfo,
    LockLost, Machine, MachineFilmAdjustment, NotAllowed, PollStatus, PrintText, Printer, Product,
    RevisionConflict, ShareError, ShareHealth, ShareUnavailable, Timings, adjustment_id,
    from_json, to_json,
)

ES = {
    "The server didn't answer within {seconds} s.": "El servidor no respondió en {seconds} s.",
    "Stripes can't reach the server.": "Stripes no puede conectarse con el servidor.",
    "No server folder is set up for Stripes on this PC.":
        "Esta PC no tiene una carpeta del servidor configurada para Stripes.",
    "Stripes can't find {folder} on the server.": "Stripes no encuentra {folder} en el servidor.",
    "Stripes isn't allowed to change {folder} on the server.":
        "Stripes no tiene permiso para modificar {folder} en el servidor.",
    "Stripes couldn't use {folder} on the server: {error}": "Stripes no pudo usar {folder} en el servidor: {error}",
    "MapleJet isn't set up on the server yet.": "MapleJet todavía no está configurado en el servidor.",
    "{file} on the server is damaged.": "{file} en el servidor está dañado.",
    "Update Stripes: {file} was saved by a newer version.":
        "Actualice Stripes: {file} se guardó con una versión más nueva.",
    "The MapleJet settings haven't loaded yet.": "La configuración de MapleJet todavía no se ha cargado.",
    "Can't reach the server since {time}.": "No se puede conectar con el servidor desde {time}.",
    "Stripes hasn't been able to read the templates from the server since {time}.":
        "Stripes no ha podido leer las plantillas del servidor desde {time}.",
    "{user} on {computer} is using this printer.": "{user} en {computer} está usando esta impresora.",
    "Another PC took over this printer's lock.": "Otra PC tomó el bloqueo de esta impresora.",
    "{user} changed this while you were editing it. Reload it and make your change again.":
        "{user} cambió esto mientras usted lo editaba. Vuelva a cargarlo y haga su cambio otra vez.",
    "Only Stripes Administrators can change this.": "Solo los Administradores de Stripes pueden cambiar esto.",
    "The file is too big (at most {mb} MB).": "El archivo es demasiado grande (máximo {mb} MB).",
    "Stripes couldn't save the log entry on this PC: {error}":
        "Stripes no pudo guardar el registro en esta PC: {error}",
}
i18n.add_spanish(ES)


# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

UPGRADES: dict[int, Callable[[str, dict], dict]] = {}   # format v -> v+1 upgrade of a decoded dict
FILE_TYPES: dict[type, str] = {
    Company: "company", Line: "line", Machine: "machine", Printer: "printer",
    MachineFilmAdjustment: "adjustment", Calibration: "calibration", Product: "product",
    Film: "film", PrintText: "print_text",
}
LOG_RESERVED_KEYS = ("format_version", "file_type", "log_kind", "logged_at", "logged_by",
                     "logged_on", "logged_pid", "log_name")
FILE_KINDS = ("front_photo", "back_photo", "artwork", "spec", "other")
RECORD_MAX_BYTES = STATUS_MAX_BYTES = 1_000_000
LOG_ENTRY_MAX_BYTES = 8_000_000
ATTACHMENT_MAX_BYTES = 50_000_000
FILM_PREVIEW_MAX_BYTES = 512_000
CHUNK_BYTES = 1_048_576
READ_RETRY_S = (0.05, 0.1, 0.2)
REPLACE_RETRY_S = (0.05, 0.1, 0.2, 0.4, 0.8, 1.6)
POOL_IDLE, POOL_MAX = 4, 16
CLOCK_BACK_TOLERANCE_S = 300.0
# 55, 1203 and 1225 aren't in storage.md's first list: CPython maps them to EINVAL, so only this set
# can tell them from a folder error (storage.md §9.2). 58 stays out: some NAS boxes give it for an
# operation they don't support, on a share that works
NETWORK_WINERRORS = frozenset({51, 53, 54, 55, 59, 64, 67, 121, 1203, 1222, 1225, 1231, 1232, 1236, 2250})

ENVELOPE_KEYS = ("format_version", "file_type")
JSON_FILE_TYPES = ("company", "changes", "line", "machine", "printer", "adjustment", "calibration",
                   "product", "film", "print_text", "poll", "expected", "line_status", "lock",
                   "heartbeat", "ack", "log", "local_copy", "user_settings", "local_state",
                   "upload_marker", "nm_alarm")

# The folders the folder script makes (storage.md §1.1). Stripes never creates these on the share
_SHARE_FOLDERS = (
    "config", "config/hx", "config/hx/lines", "config/hx/machines", "config/hx/printers",
    "config/hx/adjustments", "config/hx/calibrations",
    "templates", "templates/products", "templates/films", "templates/print-texts",
    "status", "status/deployment", "status/machines", "status/lines", "status/locks", "status/alerts",
    "status/pollers",
    "logs", "logs/hx-sends", "logs/hx-events", "logs/hx-admin",
)

# Name patterns (storage.md §2)
_ID_RE = re.compile(r"^(ln|mc|pr|pd|fm|pt)_[0-9a-f]{10}$")
_ADJ_RE = re.compile(r"^mc_[0-9a-f]{10}__fm_[0-9a-f]{10}$")
_KEY_RE = re.compile(r"^[A-Za-z0-9._-]{1,120}$")
_ALERT_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]{0,23}$")
LOG_NAME_RE = re.compile(r"^(\d{8}T\d{9}Z)\.([a-z][a-z0-9_]{0,23})\.([A-Za-z0-9_-]{1,24})\.([0-9a-f]{6})"
                         r"(?:-([2-9]))?\.json$")
_RECORD_NAME_RE = re.compile(r"^((?:ln|mc|pr|pd|fm|pt)_[0-9a-f]{10})\.json$")
_ADJ_NAME_RE = re.compile(r"^(mc_[0-9a-f]{10}__fm_[0-9a-f]{10})\.json$")
_HB_NAME_RE = re.compile(r"^([A-Za-z0-9._-]{1,120})\.json$")
_TMP_RE = re.compile(r"\.([0-9a-f]{12})-(\d+)-[0-9a-f]{6}\.tmp$")
_LOCAL_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}\.json$")
_REF_KEYS = ("send_id", "run_id", "action_id", "record_id", "film_id", "machine_id", "printer_id", "line_id")

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_RELEASED_KEEP = 200           # released tokens remembered, so this PC never mistakes one for a live lock
_IDLE_WORKER_S = 10.0          # an idle hx-io thread exits after this (real seconds)
_HANG_MAX_S = 30.0             # FlakyFileOps.hang gives up after this (real seconds)
_REACH_WRITE_S = 300.0         # reach.json is rewritten when its value is this much older

_NETWORK_ERRNOS = frozenset(getattr(errno, name) for name in
                            ("ENETUNREACH", "EHOSTUNREACH", "EHOSTDOWN", "ETIMEDOUT", "ECONNRESET",
                             "ENOTCONN", "ESTALE") if hasattr(errno, name))


class ShareDenied(ShareError):
    """A PermissionError on the share. The share stays available."""


@dataclass(kw_only=True)
class AlarmMarker:
    """status/machines/<machine_id>.nm-alarm.json (storage.md §5.9): a PC rang the NOT MONITORED alarm of
    this occurrence (states.md §7 item 7.6). Overwritten for a new occurrence, never deleted."""
    machine_id: str
    alarm_id: str
    started_at: float = 0.0             # the writer's alert started_at (its own wall clock)
    by: str = ""                        # set by write_alarm_marker
    computer: str = ""


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

@dataclass(frozen=True, kw_only=True)
class HxPaths:
    share_root: str | None       # the v2 folder on the share, or None
    roaming: str                 # %APPDATA%\Stripes\v2
    local: str                   # %LOCALAPPDATA%\Stripes\v2
    machine: str                 # %PROGRAMDATA%\Stripes\v2 (shared by the PC's users)


def v2_share_root(config_path):
    """The v2 folder next to today's config folder (or STRIPES_V2); None without a config folder."""
    override = os.environ.get("STRIPES_V2")
    if override:
        return override
    if not config_path:
        return None
    return os.path.join(os.path.dirname(os.path.normpath(config_path)), "v2")


def roaming_dir():
    override = os.environ.get("STRIPES_V2_ROAMING")
    if override:
        return override
    base = os.environ.get("APPDATA") or os.path.expanduser("~/.config")
    return os.path.join(base, "Stripes", "v2")


def local_dir():
    override = os.environ.get("STRIPES_V2_LOCAL")
    if override:
        return override
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.cache")
    return os.path.join(base, "Stripes", "v2")


def machine_dir():
    override = os.environ.get("STRIPES_V2_MACHINE")
    if override:
        return override
    base = os.environ.get("PROGRAMDATA")
    if base:
        return os.path.join(base, "Stripes", "v2")
    return os.path.join(local_dir(), "machine")


def paths(config_path):
    return HxPaths(share_root=v2_share_root(config_path), roaming=roaming_dir(), local=local_dir(),
                   machine=machine_dir())


def copy_once(old_path, new_path):
    """Copy old to new if new is missing and old exists. Never overwrites. True if it copied."""
    if not old_path or not new_path or os.path.lexists(new_path) or not os.path.isfile(old_path):
        return False
    tmp = None
    try:
        os.makedirs(os.path.dirname(os.path.abspath(new_path)), exist_ok=True)
        with open(old_path, "rb") as f:
            data = f.read()
        tmp = f"{new_path}.copy-{secrets.token_hex(3)}.tmp"
        with open(tmp, "xb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        # A rename that never replaces: another copy that got there first wins
        copied = FileOps().rename(tmp, new_path)
        if copied:
            tmp = None
        return copied
    except OSError as e:
        print(f"Could not copy {old_path} to {new_path}: {e}", file=sys.stderr)
        return False
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass


# --------------------------------------------------------------------------
# Encoding (pure)
# --------------------------------------------------------------------------

def encode_json(file_type, data):
    """{"format_version": FORMAT, "file_type": file_type, **data} as indented UTF-8 JSON.
    bytes give TypeError and NaN or infinity ValueError."""
    if file_type not in JSON_FILE_TYPES:
        raise ValueError(f"unknown file type {file_type!r}")
    obj = {"format_version": FORMAT, "file_type": file_type}
    obj.update((k, v) for k, v in data.items() if k not in ENVELOPE_KEYS)
    return (json.dumps(obj, ensure_ascii=False, indent=1, allow_nan=False) + "\n").encode("utf-8")


def _too_new(rel, found):
    return FormatTooNew(Msg(N_("Update Stripes: {file} was saved by a newer version."), {"file": rel}),
                        path=rel, found=found)


def decode_json(raw, *, file_type, rel):
    """The dict of a v2 JSON file, envelope included. ValueError when damaged, FormatTooNew when
    a newer Stripes wrote it. A BOM is accepted. Older formats are upgraded in memory."""
    try:
        d = json.loads(bytes(raw).decode("utf-8-sig"))
    except RecursionError:      # a hand-edited file nested thousands deep is damage too (§3.2)
        raise ValueError(f"{rel}: damaged (nested too deeply)") from None
    if not isinstance(d, dict):
        raise ValueError(f"{rel}: damaged (not an object)")
    version = d.get("format_version")
    if not _is_int(version) or version < 1:
        raise ValueError(f"{rel}: damaged (no format version)")
    if version > FORMAT:
        raise _too_new(rel, version)
    if file_type is not None and d.get("file_type") != file_type:
        raise ValueError(f"{rel}: damaged (not a {file_type} file)")
    for v in range(version, FORMAT):
        d = UPGRADES[v](d.get("file_type"), d)
    return d


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _finite(value):
    """value as a finite float, or None: not a number, NaN, infinity, or an int too big for a float
    (json.loads takes a 400-digit int, and float() of it raises OverflowError)."""
    if not _is_number(value):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _lenient_json(raw):
    """The object in raw, without any checks, or None (for the stable keys of a newer file)."""
    try:
        d = json.loads(bytes(raw).decode("utf-8-sig"))
    except (ValueError, RecursionError):
        return None
    return d if isinstance(d, dict) else None


def _check_id(value, what="id"):
    if not isinstance(value, str) or not _ID_RE.match(value):
        raise ValueError(f"bad {what}: {value!r}")


def record_rel_for(cls, record_id):
    """The share path of a record by its class and id: "templates/films/fm_3f9a1c22b0/film.json"."""
    if cls is Company:
        if record_id != "company":
            raise ValueError(f"the company record's id is 'company', not {record_id!r}")
        return "config/hx/company.json"
    if cls is MachineFilmAdjustment:
        if not isinstance(record_id, str) or not _ADJ_RE.match(record_id):
            raise ValueError(f"bad adjustment id: {record_id!r}")
        return f"config/hx/adjustments/{record_id}.json"
    _check_id(record_id, "record id")
    folders = {Line: "config/hx/lines", Machine: "config/hx/machines", Printer: "config/hx/printers",
               Calibration: "config/hx/calibrations", Product: "templates/products",
               PrintText: "templates/print-texts"}
    if cls is Film:
        return f"templates/films/{record_id}/film.json"
    if cls not in folders:
        raise ValueError(f"{getattr(cls, '__name__', cls)} isn't a stored record")
    return f"{folders[cls]}/{record_id}.json"


def record_rel(record):
    return record_rel_for(type(record), record.id)


def _check_file_ref(film_id, path):
    prefix = f"films/{film_id}/files/"
    name = path[len(prefix):] if isinstance(path, str) and path.startswith(prefix) else ""
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        raise ValueError(f"film file {path!r} isn't inside films/{film_id}/files/")


def _check_record(record):
    """The record's own ids agree with each other (storage.md §13.4 step 2). ValueError if not."""
    record_rel(record)
    if isinstance(record, MachineFilmAdjustment) and record.id != adjustment_id(record.machine_id, record.film_id):
        raise ValueError(f"adjustment {record.id} doesn't match its machine and film")
    if isinstance(record, Calibration) and record.id != record.machine_id:
        raise ValueError(f"calibration {record.id} doesn't match its machine")
    if isinstance(record, Film):
        for ref in record.files:
            _check_file_ref(record.id, ref.path)


def _check_plain(value, where):
    """Everything in a log entry must be plain JSON."""
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{where}: not a finite number")
        return
    if isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            _check_plain(v, f"{where}[{i}]")
        return
    if isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str):
                raise ValueError(f"{where}: keys must be text")
            _check_plain(v, f"{where}.{k}")
        return
    raise ValueError(f"{where}: {type(value).__name__} isn't JSON (store bytes as base64 under a *_b64 key)")


def _check_entry(kind, entry):
    if kind not in LOG_KINDS:
        raise ValueError(f"unknown log kind {kind!r}")
    if not isinstance(entry, dict):
        raise ValueError("a log entry is a dict")
    if not isinstance(entry.get("type"), str) or not _TYPE_RE.match(entry["type"]):
        raise ValueError(f"bad log entry type: {entry.get('type')!r}")
    reserved = [k for k in LOG_RESERVED_KEYS if k in entry]
    if reserved:
        raise ValueError(f"log entries can't use the keys {reserved}")
    _check_plain(entry, "entry")


def make_log_record(kind, entry, identity, at):
    """The stored object of a log entry: the entry plus the log envelope (storage.md §6.2)."""
    _check_entry(kind, entry)
    stored = {"format_version": FORMAT, "file_type": "log", "log_kind": kind, "logged_at": at,
              "logged_by": identity.user, "logged_on": identity.computer, "logged_pid": identity.pid}
    stored.update(entry)
    return stored


def _stamp(at):
    ms = int(at * 1000)
    return time.strftime("%Y%m%dT%H%M%S", time.gmtime(ms // 1000)) + f"{ms % 1000:03d}Z"


def _ref_part(value):
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(value))[:24]


def log_name(entry, at):
    """<stamp>.<type>.<ref>.<rand>.json, e.g. 20261029T120005123Z.send.sd_8a7b6c5d4e.4be1a9.json"""
    kind = entry.get("type")
    if not isinstance(kind, str) or not _TYPE_RE.match(kind):
        raise ValueError(f"bad log entry type: {kind!r}")
    ref = "x"
    for key in _REF_KEYS:
        value = entry.get(key)
        if value not in (None, "") and _ref_part(value):
            ref = _ref_part(value)
            break
    return f"{_stamp(at)}.{kind}.{ref}.{secrets.token_hex(3)}.json"


def log_month(name):
    return name[0:4] + "-" + name[4:6]


def encode_log(stored):
    """Two lines: the stored object, then {"end": <sha256 of line 1>}."""
    line1 = json.dumps(stored, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    raw = line1 + b"\n" + b'{"end":"' + hashlib.sha256(line1).hexdigest().encode("ascii") + b'"}\n'
    if len(raw) > LOG_ENTRY_MAX_BYTES:
        raise ValueError(f"log entry too big ({len(raw)} bytes, at most {LOG_ENTRY_MAX_BYTES})")
    return raw


def decode_log(raw):
    """The stored object of a complete log file; None if unfinished or damaged; FormatTooNew if newer."""
    raw = bytes(raw)
    if not raw.endswith(b"\n"):
        return None
    parts = raw.split(b"\n")
    if len(parts) != 3 or parts[2] != b"":
        return None
    line1, line2 = parts[0], parts[1]
    try:
        marker = json.loads(line2.decode("utf-8"))
        stored = json.loads(line1.decode("utf-8"))
    except (ValueError, RecursionError):
        return None
    if not (isinstance(marker, dict) and len(marker) == 1 and isinstance(marker.get("end"), str)):
        return None
    if marker["end"] != hashlib.sha256(line1).hexdigest():
        return None
    if not isinstance(stored, dict) or stored.get("file_type") != "log":
        return None
    version = stored.get("format_version")
    if not _is_int(version) or version < 1:
        return None
    if version > FORMAT:
        raise _too_new(f"logs/{stored.get('log_kind', '')}", version)
    for v in range(version, FORMAT):
        stored = UPGRADES[v]("log", stored)
    return stored


def safe_file_name(name):
    """A film original's name as it may appear on the share (storage.md §2)."""
    base = str(name).replace("\\", "/").rsplit("/", 1)[-1]
    stem, ext = os.path.splitext(base)
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", stem)[:48] + re.sub(r"[^A-Za-z0-9.]+", "", ext)[:10]
    # Windows drops trailing dots, which would change the name the record points at
    safe = safe.rstrip(".")
    return safe or "file"


def score(printer_id, computer):
    """Rendezvous score of one printer for one PC (storage.md §12.4)."""
    digest = hashlib.sha256(f"{printer_id}|{computer.upper()}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def assign(printer_ids, heartbeats):
    """printer_id -> keys of the live pollers able to poll, in rank order (rank 0 polls).
    Every PC gets the same answer from the same inputs. A PC is left out for the printers its heartbeat
    lists in `declined` (its worker for that printer is stuck, states.md §6.2 item 3.0), as can_poll leaves
    out a whole PC."""
    pollers = {}
    for hb in heartbeats:
        if hb.can_poll:
            pollers.setdefault(hb.key, hb)            # one entry per key
    return {pid: [hb.key for hb in sorted((hb for hb in pollers.values() if pid not in (hb.declined or ())),
                                          key=lambda hb: (-score(pid, hb.computer), hb.key))]
            for pid in printer_ids}


def takeover_after_s(rank, timings=TIMINGS):
    """How stale a printer's poll.json must be before the PC at this rank starts polling it."""
    first, second = timings.takeover_s[0], timings.takeover_s[1]
    if rank <= 0:
        return 0.0
    if rank == 1:
        return float(first)
    return float(second + (second - first) * (rank - 2))


def settle_s(timings=TIMINGS):
    """A starting poller's first request waits for a lock read this long after its first write."""
    return 0.4 * timings.stale_s


def write_record(store, record):
    """Write a record file as given: no revision bump, no permission check, no log, no
    changes.json bump. For seeding test worlds (FakeWorld) only. A test world has no folder
    script, so this also makes the share's folders."""
    _check_record(record)
    rel = record_rel(record)
    store._make_share_folders(rel.rsplit("/", 1)[0])
    store.write_json(rel, {"file_type": FILE_TYPES[type(record)], **to_json(record)})


def self_check():
    """Run the file operations once in a temp folder, as the exe's --self-check does; raises on failure."""
    folder = tempfile.mkdtemp(prefix="stripes-hx-share-")
    try:
        fs = FileOps()
        fs.tmp_tag = f"selfcheck-{os.getpid()}"
        path = os.path.join(folder, "state.json")
        fs.write_whole(path, encode_json("local_state", {"n": 1}))
        fs.write_whole(path, encode_json("local_state", {"n": 2}))
        d = decode_json(fs.read_bytes(path), file_type="local_state", rel="state.json")
        if d.get("n") != 2:
            raise RuntimeError("write_whole didn't replace the file")
        ident = Identity(user="SELF\\check", computer="SELFCHECK", sids=frozenset(), pid=os.getpid())
        stored = make_log_record("hx-events", {"type": "self_check", "text": "Stripes ñ"}, ident, time.time())
        raw = encode_log(stored)
        log = os.path.join(folder, "entry.json")
        if not fs.create_exclusive(log, raw):
            raise RuntimeError("create_exclusive didn't create the file")
        if fs.create_exclusive(log, raw):
            raise RuntimeError("create_exclusive created a file that exists")
        if decode_log(fs.read_bytes(log)) != stored:
            raise RuntimeError("the log entry didn't read back")
        moved = os.path.join(folder, "moved.json")
        if not fs.rename(log, moved) or fs.rename(log, moved) or not fs.exists(moved) or fs.exists(log):
            raise RuntimeError("rename didn't work")
        left = [n for n in os.listdir(folder) if n.endswith(".tmp")]
        if left:
            raise RuntimeError(f"temp files left behind: {left}")
    finally:
        shutil.rmtree(folder, ignore_errors=True)


# --------------------------------------------------------------------------
# FileOps: the raw file operations
# --------------------------------------------------------------------------

class _TempFile:
    """A temp file being written chunk by chunk (copy_in)."""

    def __init__(self, f, tmp):
        self.f = f
        self.tmp = tmp


def _network_cause(e, path):
    """storage.md §8.2 note 9: on Windows, os.open, open(), their writes and every read and seek go through the
    C runtime, whose OSError carries only an errno, so a lost server (winerror 53, 64, 1231, ...) reads as
    ENOENT or EINVAL, a folder error. The server is then asked once more through CreateFileW
    (hx_win.open_for_read), whose error has the winerror: that error when it is a network one, else None (the
    first error stands)."""
    if not hx_win.is_windows() or getattr(e, "winerror", None) is not None \
            or isinstance(e, (FileExistsError, PermissionError)):
        return None
    try:
        with hx_win.open_for_read(path):
            pass
    except OSError as again:
        if getattr(again, "winerror", None) in NETWORK_WINERRORS:
            return again
    return None


def _crt_call(path, fn, *args):
    """fn(*args), a FileOps step that goes through the C runtime on Windows, with _network_cause."""
    try:
        return fn(*args)
    except OSError as e:
        cause = _network_cause(e, path)
        if cause is None:
            raise
        raise cause from e


def _end_offset(f):
    f.seek(0, os.SEEK_END)
    return f.tell()


class FileOps:
    """The raw file operations ShareStore uses; replaced in tests to fake a slow or dead share.
    Holds no state apart from tmp_tag (and the retry delays and undo_create_s, which ShareStore sets from
    its timings). Paths are absolute."""

    tmp_tag: str = ""
    read_retry_s = READ_RETRY_S
    create_retry_s = READ_RETRY_S
    replace_retry_s = REPLACE_RETRY_S
    undo_create_s = TIMINGS.lock_stale_s / 2

    def read_bytes(self, path):
        delays = list(self.read_retry_s)
        while True:
            try:
                with hx_win.open_for_read(path) as f:
                    return _crt_call(path, f.read)      # CreateFileW opens, the C runtime reads
            except FileNotFoundError:
                raise
            except PermissionError:
                # A sharing violation from a program that doesn't share delete: it goes away
                if not delays:
                    raise
                time.sleep(delays.pop(0))

    def write_whole(self, path, data):
        _crt_call(path, self._write_whole, path, data)

    def _write_whole(self, path, data):
        handle = self._do_open_temp(path)
        try:
            handle.f.write(data)
        except BaseException:
            self._do_discard(handle)
            raise
        self._do_commit(handle, path)

    def create_exclusive(self, path, data):
        return _crt_call(path, self._create_exclusive, path, data)

    def _create_exclusive(self, path, data):
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        delays = list(self.create_retry_s)
        while True:
            opened = time.monotonic()           # no one can see the file before this
            try:
                fd = os.open(path, flags, 0o666)
                break
            except FileExistsError:
                return False
            except PermissionError:
                # Windows refuses a create while a just-removed file is still open somewhere
                if not delays:
                    raise
                time.sleep(delays.pop(0))
        try:
            view = memoryview(bytes(data))
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            _not_abandoned()
        except _Abandoned:
            # The caller gave up (the share timeout) and was told the share is down: a lock it doesn't know
            # it holds would keep every other PC out. Take the file back while no other PC can have judged it
            # stale and taken it over (storage.md §8.1); after that it is left to go stale
            if time.monotonic() - opened < self.undo_create_s:
                try:
                    os.remove(path)
                except OSError:
                    pass
            raise
        return True

    def rename(self, src, dst):
        """Never replaces: False if src is gone or dst exists."""
        try:
            if hx_win.is_windows():
                os.rename(src, dst)
                return True
            os.link(src, dst)
        except (FileNotFoundError, FileExistsError):
            return False
        # link and unlink aren't one step: when two renames of src meet, both links work but only
        # one src can go. The other (src gone, or already a different file) takes its link back.
        try:
            if not os.path.samestat(os.stat(src), os.stat(dst)):
                raise FileNotFoundError(errno.ENOENT, "moved meanwhile", src)
            os.unlink(src)
        except FileNotFoundError:
            try:
                os.unlink(dst)
            except OSError:
                pass
            return False
        return True

    def listdir(self, path):
        return os.listdir(path)

    def makedirs(self, path):
        os.makedirs(path, exist_ok=True)

    def remove(self, path):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass

    def exists(self, path):
        """Opens the file by name; never stat (the SMB client answers those from its caches)."""
        try:
            with hx_win.open_for_read(path):
                return True
        except PermissionError:
            return True
        except FileNotFoundError:
            return False

    # Used by ShareStore only: folders below one the folder script made, sizes, and copy_in's chunks

    def _mkdirs_below(self, base, path):
        if not os.path.isdir(base):
            raise FileNotFoundError(errno.ENOENT, "No such folder", base)
        os.makedirs(path, exist_ok=True)

    def _size(self, path):
        with hx_win.open_for_read(path) as f:
            return _crt_call(path, _end_offset, f)

    def _open_temp(self, path):
        return _crt_call(path, self._do_open_temp, path)

    def _write_chunk(self, handle, data):
        _crt_call(handle.tmp, handle.f.write, data)

    def _commit_temp(self, handle, path):
        _crt_call(path, self._do_commit, handle, path)

    def _discard_temp(self, handle):
        self._do_discard(handle)

    def _replace(self, src, dst):
        os.replace(src, dst)

    def _do_open_temp(self, path):
        tmp = f"{path}.{self.tmp_tag}-{secrets.token_hex(3)}.tmp"
        return _TempFile(open(tmp, "xb"), tmp)

    def _do_commit(self, handle, path):
        try:
            handle.f.flush()
            os.fsync(handle.f.fileno())
            handle.f.close()
            delays = list(self.replace_retry_s)
            while True:
                # A write whose caller gave up (the share timeout) never commits (storage.md §8.1): the
                # file stays as the caller was told, and can't land over a newer owner's file later
                _not_abandoned()
                try:
                    self._replace(handle.tmp, path)
                    return
                except PermissionError:
                    # A reader without share-delete (antivirus, backup, Explorer's preview)
                    if not delays:
                        raise
                    time.sleep(delays.pop(0))
        except BaseException:
            self._do_discard(handle)
            raise

    def _do_discard(self, handle):
        try:
            handle.f.close()
        except OSError:
            pass
        try:
            os.remove(handle.tmp)
        except OSError:
            pass


class _Cell:
    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value


class _FlakyShared:
    """What every view of one FlakyFileOps shares: the creation-time register and its lock."""

    def __init__(self, clock):
        self.clock = clock
        self.lock = threading.Lock()
        self.created = {}           # normalised path -> (clock.monotonic(), view number)
        self.views = itertools.count(1)


def _switch(name):
    def get(self):
        return self._cells[name].value

    def put(self, value):
        # The root's switches are shared by every view that didn't get its own. A view that
        # changes a switch it shares gets its own from then on, so one simulated PC's faults
        # never leak to the others.
        if not self._root and name not in self._owned:
            self._cells[name] = _Cell(value)
            self._owned.add(name)
        else:
            self._cells[name].value = value
    return property(get, put)


_SWITCH_DEFAULTS = {"down": False, "hang": False, "slow_s": 0.0, "hide_new_s": 0.0, "not_found_cache_s": 0.0,
                    "fail_replace": 0, "break_after_create": 0}


class FlakyFileOps(FileOps):
    """A real FileOps on real files with switchable faults, for tests (storage.md §16)."""

    down = _switch("down")
    hang = _switch("hang")
    slow_s = _switch("slow_s")
    hide_new_s = _switch("hide_new_s")
    not_found_cache_s = _switch("not_found_cache_s")
    fail_replace = _switch("fail_replace")
    break_after_create = _switch("break_after_create")

    def __init__(self, *, clock: Clock = SYSTEM_CLOCK):
        self._shared = _FlakyShared(clock)
        self._cells = {name: _Cell(value) for name, value in _SWITCH_DEFAULTS.items()}
        self._owned = set(_SWITCH_DEFAULTS)
        self._root = True
        self._setup_view()

    def _setup_view(self):
        self._view = next(self._shared.views)
        self._not_found = {}        # normalised path -> clock.monotonic() it stays "not found" until
        self.calls = []

    @property
    def clock(self):
        return self._shared.clock

    def view(self, *, down=None, hang=None, slow_s=None):
        """A view on the same files for one simulated PC: its own not-found cache and calls, the
        parent's switches unless given here (a value gives this view its own switch)."""
        child = FlakyFileOps.__new__(FlakyFileOps)
        child._shared = self._shared
        child._cells = dict(self._cells)
        child._owned = set()
        child._root = False
        for name, value in (("down", down), ("hang", hang), ("slow_s", slow_s)):
            if value is not None:
                child._cells[name] = _Cell(value)
                child._owned.add(name)
        child._setup_view()
        return child

    # faults

    def _enter(self, op, path):
        self.calls.append((op, path))
        if self.down:
            raise OSError(errno.EHOSTDOWN, "share down (fake)")
        if self.hang:
            started = time.monotonic()
            while self.hang and time.monotonic() - started < _HANG_MAX_S:
                time.sleep(0.005)
            raise OSError(errno.EHOSTDOWN, "share down (fake)")
        slow = self.slow_s
        if slow > 0:
            time.sleep(slow)

    def _take(self, name):
        """Use up one of a counted fault; True if there was one."""
        with self._shared.lock:
            cell = self._cells[name]
            if cell.value > 0:
                cell.value -= 1
                return True
            return False

    def _give_back(self, name):
        with self._shared.lock:
            self._cells[name].value += 1

    def _cached_not_found(self, path):
        key = os.path.normpath(path)
        with self._shared.lock:
            until = self._not_found.get(key)
            if until is None:
                return False
            if self.clock.monotonic() < until:
                return True
            del self._not_found[key]
            return False

    def _note_not_found(self, path):
        seconds = self.not_found_cache_s
        if seconds > 0:
            with self._shared.lock:
                self._not_found[os.path.normpath(path)] = self.clock.monotonic() + seconds

    def _created(self, paths_made):
        now = self.clock.monotonic()
        with self._shared.lock:
            for p in paths_made:
                key = os.path.normpath(p)
                self._shared.created[key] = (now, self._view)
                self._not_found.pop(key, None)     # this PC knows about its own new file

    @staticmethod
    def _missing_dirs(path):
        out = []
        p = os.path.normpath(path)
        while p and not os.path.lexists(p):
            out.append(p)
            parent = os.path.dirname(p)
            if parent == p:
                break
            p = parent
        return out

    # operations

    def read_bytes(self, path):
        self._enter("read_bytes", path)
        if self._cached_not_found(path):
            raise FileNotFoundError(errno.ENOENT, "not found (cached)", path)
        try:
            return super().read_bytes(path)
        except FileNotFoundError:
            self._note_not_found(path)
            raise

    def exists(self, path):
        self._enter("exists", path)
        if self._cached_not_found(path):
            return False
        found = super().exists(path)
        if not found:
            self._note_not_found(path)
        return found

    def _size(self, path):
        self._enter("size", path)
        if self._cached_not_found(path):
            raise FileNotFoundError(errno.ENOENT, "not found (cached)", path)
        try:
            return super()._size(path)
        except FileNotFoundError:
            self._note_not_found(path)
            raise

    def write_whole(self, path, data):
        self._enter("write_whole", path)
        new = not os.path.lexists(path)
        super().write_whole(path, data)
        if new:
            self._created([path])

    def create_exclusive(self, path, data):
        self._enter("create_exclusive", path)
        if self._take("break_after_create"):
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
            try:
                fd = os.open(path, flags, 0o666)
            except FileExistsError:
                self._give_back("break_after_create")
                return False
            except OSError:
                self._give_back("break_after_create")     # only a file that was created counts
                raise
            try:
                os.write(fd, bytes(data)[:len(data) // 2])
            finally:
                os.close(fd)
            self._created([path])
            raise OSError(errno.EIO, "write failed half way (fake)", path)
        made = super().create_exclusive(path, data)
        if made:
            self._created([path])
        return made

    def rename(self, src, dst):
        self._enter("rename", src)
        moved = super().rename(src, dst)
        if moved:
            self._created([dst])
        return moved

    def listdir(self, path):
        self._enter("listdir", path)
        names = super().listdir(path)
        hide = self.hide_new_s
        if hide <= 0:
            return names
        now = self.clock.monotonic()
        with self._shared.lock:
            created = dict(self._shared.created)
        shown = []
        for name in names:
            made = created.get(os.path.normpath(os.path.join(path, name)))
            if made and made[1] != self._view and now - made[0] < hide:
                continue        # like the SMB directory cache: another PC's new file isn't listed yet
            shown.append(name)
        return shown

    def makedirs(self, path):
        self._enter("makedirs", path)
        new = self._missing_dirs(path)
        super().makedirs(path)
        self._created(new)

    def _mkdirs_below(self, base, path):
        self._enter("makedirs", path)
        new = self._missing_dirs(path)
        super()._mkdirs_below(base, path)
        self._created(new)

    def remove(self, path):
        self._enter("remove", path)
        super().remove(path)

    def _open_temp(self, path):
        self._enter("copy_open", path)
        return super()._open_temp(path)

    def _write_chunk(self, handle, data):
        self._enter("copy_chunk", handle.tmp)
        super()._write_chunk(handle, data)

    def _commit_temp(self, handle, path):
        self._enter("copy_commit", path)
        new = not os.path.lexists(path)
        super()._commit_temp(handle, path)
        if new:
            self._created([path])

    def _discard_temp(self, handle):
        self._enter("copy_discard", handle.tmp)
        super()._discard_temp(handle)

    def _replace(self, src, dst):
        if self._take("fail_replace"):
            raise PermissionError(errno.EACCES, "replace refused (fake)", dst)
        super()._replace(src, dst)


# --------------------------------------------------------------------------
# The hx-io pool
# --------------------------------------------------------------------------

class _TimedOut(Exception):
    pass


class _Abandoned(Exception):
    """Raised inside an hx-io job whose caller has given up: the write it was making never commits."""


_CURRENT = threading.local()       # .job: the _Job this hx-io thread is running, if any


def _not_abandoned():
    """In FileOps, right before a write commits: _Abandoned when the hx-io job running it was given up.
    Outside the pool (FileOps called directly) it does nothing."""
    job = getattr(_CURRENT, "job", None)
    if job is not None:
        with job.lock:
            if job.abandoned:
                raise _Abandoned()


class _Job:
    __slots__ = ("fn", "args", "result", "error", "done", "lock", "finished", "abandoned")

    def __init__(self, fn, args):
        self.fn = fn
        self.args = args
        self.result = None
        self.error = None
        self.done = threading.Event()
        self.lock = threading.Lock()
        self.finished = False
        self.abandoned = False

    def run(self):
        """Runs the call; True when the caller gave up on it (the thread then leaves the pool)."""
        try:
            self.result = self.fn(*self.args)
        except BaseException as e:
            self.error = e
        with self.lock:
            self.finished = True
            abandoned = self.abandoned
        self.done.set()
        return abandoned

    def wait(self, timeout_s):
        if not self.done.wait(timeout_s):
            with self.lock:
                if not self.finished:
                    self.abandoned = True
                    raise _TimedOut()
        if self.error is not None:
            raise self.error
        return self.result


class _Worker:
    __slots__ = ("event", "job")

    def __init__(self):
        self.event = threading.Event()
        self.job = None


_IO_SERIAL = itertools.count(1)


class _IoPool:
    """hx-io threads: a call starts at once on an idle thread or a new one. A thread whose call
    timed out finishes its OS call and exits instead of rejoining the pool."""

    def __init__(self, idle_max=POOL_IDLE, max_threads=POOL_MAX):
        self.idle_max = idle_max
        self.max_threads = max_threads
        self._lock = threading.Lock()
        self._idle = []
        self.alive = 0
        self.peak = 0

    def start(self, fn, *args):
        """The started job, or None when every thread is busy."""
        job = _Job(fn, args)
        with self._lock:
            if self._idle:
                worker = self._idle.pop()
                worker.job = job
                worker.event.set()
                return job
            if self.alive >= self.max_threads:
                return None
            self.alive += 1
            self.peak = max(self.peak, self.alive)
            name = f"hx-io-{next(_IO_SERIAL)}"
        try:
            threading.Thread(target=self._work, args=(job,), name=name, daemon=True).start()
        except RuntimeError:
            with self._lock:
                self.alive -= 1
            return None
        return job

    def _work(self, job):
        worker = _Worker()
        while job is not None:
            _CURRENT.job = job              # so FileOps can see that its caller gave up (_not_abandoned)
            try:
                abandoned = job.run()
            finally:
                _CURRENT.job = None
            if abandoned:
                break
            job = self._next(worker)
            if job is None:
                return
        with self._lock:
            self.alive -= 1

    def _next(self, worker):
        with self._lock:
            if len(self._idle) >= self.idle_max:
                self.alive -= 1
                return None
            worker.job = None
            worker.event.clear()
            self._idle.append(worker)
        worker.event.wait(_IDLE_WORKER_S)
        with self._lock:
            if worker.job is None:
                self._idle.remove(worker)
                self.alive -= 1
                return None
            return worker.job


# --------------------------------------------------------------------------
# Freshness
# --------------------------------------------------------------------------

class _Seen:
    __slots__ = ("counter", "changed_at", "missing_since", "touched")

    def __init__(self, counter, now):
        self.counter = counter
        self.changed_at = now
        self.missing_since = None
        self.touched = now


class CounterWatch:
    """Judges freshness by change, on this PC's own monotonic clock."""

    PRUNE_S = 3600.0

    def __init__(self, clock: Clock):
        self.clock = clock
        self._lock = threading.Lock()
        self._keys = {}

    def see(self, key, counter):
        """Seconds since this counter last changed (0.0 on a change, a first sight or a return)."""
        now = self.clock.monotonic()
        with self._lock:
            self._prune(now)
            seen = self._keys.get(key)
            if seen is None or seen.missing_since is not None or seen.counter != counter:
                self._keys[key] = _Seen(counter, now)
                return 0.0
            seen.touched = now
            return now - seen.changed_at

    def age(self, key):
        """The same age without a new reading; while missing, how long it has been missing."""
        now = self.clock.monotonic()
        with self._lock:
            seen = self._keys.get(key)
            if seen is None:
                return None
            if seen.missing_since is not None:
                return now - seen.missing_since
            return now - seen.changed_at

    def missing(self, key):
        """Marks the key missing; returns how long it has been missing without a break."""
        now = self.clock.monotonic()
        with self._lock:
            seen = self._keys.get(key)
            if seen is None:
                seen = self._keys[key] = _Seen(None, now)
            if seen.missing_since is None:
                seen.missing_since = now
            seen.touched = now
            return now - seen.missing_since

    def forget(self, key):
        with self._lock:
            self._keys.pop(key, None)

    def _prune(self, now):
        old = [k for k, s in self._keys.items() if now - s.touched >= self.PRUNE_S]
        for k in old:
            del self._keys[k]


# --------------------------------------------------------------------------
# ShareStore
# --------------------------------------------------------------------------

def _network_error(e):
    return getattr(e, "winerror", None) in NETWORK_WINERRORS or e.errno in _NETWORK_ERRNOS


def _error_text(e):
    return f"{type(e).__name__}: {e.strerror or e}"


def _folder_of(rel):
    return rel.rsplit("/", 1)[0] if "/" in rel else "."


def _held_msg(info):
    return Msg(N_("{user} on {computer} is using this printer."), {"user": info.user, "computer": info.computer})


def _unknown_holder(printer_id):
    return LockInfo(printer_id=printer_id, token="", user="?", computer="?", pid=0, purpose="unknown")


class ShareStore:
    """Every share access. Each operation runs on an 'hx-io' worker thread (a small pool) and is
    abandoned after share_timeout_s (ShareUnavailable); a stuck worker is replaced. Then the share
    counts as down and calls fail at once until a background probe succeeds (every share_retry_s).
    Thread-safe."""

    def __init__(self, root, *, identity: Identity, clock: Clock = SYSTEM_CLOCK, fs: FileOps | None = None,
                 timings: Timings = TIMINGS, version: str = "dev"):
        self.root = root or None
        self.identity = identity
        self.clock = clock
        self.fs = fs if fs is not None else FileOps()
        self.timings = timings
        self.version = version
        self._tag = hashlib.sha256(f"{identity.computer}|{identity.user}".encode("utf-8")).hexdigest()[:12]
        self.fs.tmp_tag = f"{self._tag}-{identity.pid}"
        # The retries must fit inside the share timeout, also with scaled test timings
        scale = timings.share_timeout_s / TIMINGS.share_timeout_s
        self.fs.read_retry_s = self.fs.create_retry_s = tuple(d * scale for d in READ_RETRY_S)
        self.fs.replace_retry_s = tuple(d * scale for d in REPLACE_RETRY_S)
        self.fs.undo_create_s = timings.lock_stale_s / 2
        self.watch = CounterWatch(clock)
        self.local_locks: dict[str, str] = {}
        self.available = self.root is not None
        self.last_reached = None
        self.down_reason = None if self.root else Msg(N_("No server folder is set up for Stripes on this PC."))
        self._lock = threading.Lock()
        self._pool = _IoPool()
        self._probe_after = 0.0
        self._probing = False
        self._probe_failed_at = None         # clock.monotonic() when the last probe failed (reach_now)
        self._cleaned = False
        self._housekeeping = False
        self._local_lock_infos = {}
        self._pending_releases = {}          # token -> (printer_id, until): removals still to retry
        # token -> (printer_id, until) of locks this store released; until = that lock's last_ok +
        # lock_stale_s / 2, the end of the time its file may be removed by name (storage.md §11.5)
        self._released = collections.OrderedDict()
        # printer_id -> threading.Lock, held by acquire() while it works on the share and by a pending
        # release's read-then-remove, so that remove can't land on this PC's own new lock (§11.5)
        self._printer_mutexes = {}
        self._expected = {}                  # machine_id -> the last ExpectedState read or written
        self._max_poll = {}                  # machine_id -> the highest poll counter written or read
        self._polls = {}                     # machine_id -> the newest PollStatus read (any reader)
        self._acks = {}
        self._lock_tokens = {}               # printer_id -> token of the last lock read, or None
        self._heartbeat = Heartbeat(self)

    # paths and running operations

    def _path(self, rel):
        if not isinstance(rel, str) or not rel or rel.startswith("/") or "\\" in rel or ":" in rel:
            raise ValueError(f"bad share path {rel!r}")
        parts = rel.split("/")
        if any(p in ("", ".", "..") for p in parts):
            raise ValueError(f"bad share path {rel!r}")
        if self.root is None:
            return None
        return os.path.join(self.root, *parts)

    def full_path(self, rel):
        return self._path(rel)

    def _timeout_msg(self):
        seconds = self.timings.share_timeout_s
        return Msg(N_("The server didn't answer within {seconds} s."),
                   {"seconds": int(seconds) if seconds == int(seconds) else round(seconds, 2)})

    def _call(self, fn, *args):
        """One FileOps call on an hx-io thread with the share timeout. OSErrors other than the
        network ones are raised as they are, for the caller to map."""
        if self.root is None:
            raise ShareUnavailable(self.down_reason)
        with self._lock:
            if not self.available:
                self._start_probe(due_only=True)
                raise ShareUnavailable(self.down_reason)
        job = self._pool.start(fn, *args)
        if job is None:
            raise ShareUnavailable(self._timeout_msg())
        try:
            result = job.wait(self.timings.share_timeout_s)
        except _TimedOut:
            msg = self._timeout_msg()
            self._mark_down(msg)
            raise ShareUnavailable(msg) from None
        except OSError as e:
            if _network_error(e):
                msg = Msg(N_("Stripes can't reach the server."))
                self._mark_down(msg)
                raise ShareUnavailable(msg) from None
            if isinstance(e, FileNotFoundError):
                self._reached()         # the server answered
            raise
        self._reached()
        return result

    def _mapped(self, rel, e, *, folder=None):
        folder = folder or _folder_of(rel)
        if isinstance(e, FileNotFoundError):
            return ShareError(Msg(N_("Stripes can't find {folder} on the server."), {"folder": folder}))
        if isinstance(e, PermissionError):
            return ShareDenied(Msg(N_("Stripes isn't allowed to change {folder} on the server."), {"folder": folder}))
        return ShareError(Msg(N_("Stripes couldn't use {folder} on the server: {error}"),
                              {"folder": folder, "error": _error_text(e)}))

    def _read_op(self, rel, fn, *args):
        """A read: None when the file is missing."""
        try:
            return self._call(fn, *args)
        except FileNotFoundError:
            return None
        except OSError as e:
            raise self._mapped(rel, e) from None

    def _write_op(self, rel, fn, *args, folder=None):
        try:
            return self._call(fn, *args)
        except OSError as e:
            raise self._mapped(rel, e, folder=folder) from None

    def _mark_down(self, reason):
        with self._lock:
            self.down_reason = reason
            if self.available:
                self.available = False
                self._probe_after = self.clock.monotonic() + self.timings.share_retry_s

    def _start_probe(self, *, due_only):
        """Lock held. Starts a probe on an hx-io thread when one is due (or straight away)."""
        if self.root is None or self._probing:
            return
        now = self.clock.monotonic()
        if due_only and now < self._probe_after:
            return
        self._probe_after = now + self.timings.share_retry_s
        self._probing = True
        if self._pool.start(self._probe) is None:
            self._probing = False

    def probe_now(self):
        """Non-blocking: probe the share straight away (the Retry button)."""
        with self._lock:
            self._start_probe(due_only=False)

    def reach_now(self):
        """Blocking, at most share_timeout_s: while the share is marked down, one probe now instead of at
        the next due time (§9.3 item 6). True when the share is available afterwards. A probe that failed
        less than share_timeout_s ago isn't repeated, so in a real outage the next caller fails at once."""
        if self.root is None:
            return False
        with self._lock:
            if self.available:
                return True
            failed = self._probe_failed_at
            if failed is not None and self.clock.monotonic() - failed < self.timings.share_timeout_s:
                return False
            self._probe_after = self.clock.monotonic() + self.timings.share_retry_s
        ok, reason = self._list_root()
        self._probed(ok, reason)
        with self._lock:
            return self.available

    def _probe(self):
        """On an hx-io thread: listdir(root) as one more timed operation. A listing that hangs is
        abandoned after share_timeout_s like any other, so the next probe can still start."""
        ok, reason = self._list_root()
        self._probed(ok, reason, background=True)

    def _probed(self, ok, reason, *, background=False):
        with self._lock:
            if background:
                self._probing = False
            if ok:
                self.available = True
                self.down_reason = None
                self._probe_failed_at = None
            else:
                self._probe_failed_at = self.clock.monotonic()
        if ok:
            self._reached()
        else:
            self._mark_down(reason)

    def _list_root(self):
        """(ok, reason) of one listdir(root) with the share timeout."""
        ok, reason = False, None
        try:
            job = self._pool.start(self.fs.listdir, self.root)
            if job is None:
                raise _TimedOut()
            job.wait(self.timings.share_timeout_s)
            ok = True
        except _TimedOut:
            reason = self._timeout_msg()
        except OSError as e:
            # Network errors first: Windows reports a missing server as "not found" too
            if _network_error(e):
                reason = Msg(N_("Stripes can't reach the server."))
            elif isinstance(e, PermissionError):
                ok = True               # the server answered
            elif isinstance(e, FileNotFoundError):
                reason = Msg(N_("Stripes can't find {folder} on the server."), {"folder": self.root})
            else:
                reason = Msg(N_("Stripes couldn't use {folder} on the server: {error}"),
                             {"folder": self.root, "error": _error_text(e)})
        except Exception:
            traceback.print_exc()
            reason = Msg(N_("Stripes can't reach the server."))
        return ok, reason

    def _reached(self):
        """After every successful operation: note the time, and do the one-off and pending clean-ups."""
        self.last_reached = self.clock.time()
        with self._lock:
            if self._housekeeping:
                return
            clean = not self._cleaned
            releases = bool(self._pending_releases)
            if not (clean or releases):
                return
            self._housekeeping = True
            self._cleaned = True
        try:
            if clean:
                self._clean_temp_files()
            if releases:
                self._retry_releases()
        except ShareError:
            pass
        except Exception:
            traceback.print_exc()
        finally:
            with self._lock:
                self._housekeeping = False

    def _clean_temp_files(self):
        # Temp files left by an earlier crashed run of this user on this PC (SingleInstance means
        # no other live process has this prefix)
        for folder in ("status/machines", "status/lines", "status/locks", "status/pollers"):
            for name in self.listdir(folder):
                m = _TMP_RE.search(name)
                if m and m.group(1) == self._tag and int(m.group(2)) != self.identity.pid:
                    self.remove(f"{folder}/{name}")

    def _make_share_folders(self, *extra):
        """The folder script's folders, for test worlds (write_record) only."""
        for rel in _SHARE_FOLDERS + extra:
            self._write_op(rel, self.fs.makedirs, self._path(rel))

    # generic

    def read_bytes(self, rel):
        return self._read_op(rel, self.fs.read_bytes, self._path(rel))

    def write_bytes(self, rel, data):
        self._write_op(rel, self.fs.write_whole, self._path(rel), bytes(data))

    def create_once(self, rel, data):
        return self._write_op(rel, self.fs.create_exclusive, self._path(rel), bytes(data))

    def listdir(self, rel):
        names = self._read_op(rel, self.fs.listdir, self._path(rel))
        return sorted(names) if names is not None else []

    def makedirs(self, rel):
        """Makes the folders of rel below the deepest one the folder script makes; that one must exist."""
        path = self._path(rel)
        parts = rel.split("/")
        base = ""
        for i in range(len(parts), 0, -1):
            if "/".join(parts[:i]) in _SHARE_FOLDERS:
                base = "/".join(parts[:i])
                break
        base_path = self._path(base) if base else self.root
        self._write_op(rel, self.fs._mkdirs_below, base_path, path, folder=base or self.root)

    def remove(self, rel):
        self._write_op(rel, self.fs.remove, self._path(rel))

    def _rename(self, src_rel, dst_rel):
        return self._write_op(dst_rel, self.fs.rename, self._path(src_rel), self._path(dst_rel))

    def _size(self, rel):
        return self._read_op(rel, self.fs._size, self._path(rel))

    def copy_in(self, rel, source_path):
        """A whole-file copy of a local file onto the share, one share operation per 1 MiB."""
        path = self._path(rel)
        handle = self._write_op(rel, self.fs._open_temp, path)
        try:
            with open(source_path, "rb") as src:
                while True:
                    chunk = src.read(CHUNK_BYTES)
                    if not chunk:
                        break
                    self._write_op(rel, self.fs._write_chunk, handle, chunk)
            self._write_op(rel, self.fs._commit_temp, handle, path)
        except BaseException:
            try:
                self._call(self.fs._discard_temp, handle)
            except Exception:
                pass
            raise

    def read_json(self, rel):
        """decode_json with any file_type; None if missing. FormatTooNew or ValueError (damaged)."""
        raw = self.read_bytes(rel)
        if raw is None:
            return None
        if len(raw) > RECORD_MAX_BYTES:
            raise ValueError(f"{rel}: damaged (too big)")
        return decode_json(raw, file_type=None, rel=rel)

    def write_json(self, rel, data):
        """data must contain file_type; written whole and swapped in."""
        if not isinstance(data, dict) or "file_type" not in data:
            raise ValueError("write_json needs a file_type")
        raw = encode_json(data["file_type"], data)
        if len(raw) > STATUS_MAX_BYTES:
            raise ValueError(f"{rel}: too big ({len(raw)} bytes)")
        self.write_bytes(rel, raw)

    def _read_status(self, rel, file_type):
        """("ok", dict) | ("newer", (stable dict, FormatTooNew)) | ("missing", None) | ("damaged", None)"""
        raw = self.read_bytes(rel)
        if raw is None:
            return "missing", None
        if len(raw) > STATUS_MAX_BYTES:
            return "damaged", None
        try:
            return "ok", decode_json(raw, file_type=file_type, rel=rel)
        except FormatTooNew as e:
            return "newer", (_lenient_json(raw) or {}, e)
        except ValueError:
            return "damaged", None

    # status: poll

    def _note_poll_counter(self, machine_id, counter):
        with self._lock:
            if counter > self._max_poll.get(machine_id, counter - 1):
                self._max_poll[machine_id] = counter

    def read_poll(self, machine_id):
        _check_id(machine_id, "machine id")
        rel, key = f"status/machines/{machine_id}.poll.json", "poll:" + machine_id
        kind, d = self._read_status(rel, "poll")
        if kind == "newer":
            stable, err = d
            counter = stable.get("counter")
            if _is_int(counter):
                self.watch.see(key, counter)
                self._note_poll_counter(machine_id, counter)
            else:
                self.watch.missing(key)
            raise err
        if kind == "ok":
            try:
                status = from_json(PollStatus, d)
                if status.machine_id != machine_id:
                    raise ValueError("machine id differs from the file name")
            except ValueError:
                pass
            else:
                self.watch.see(key, status.counter)
                self._note_poll_counter(machine_id, status.counter)
                with self._lock:
                    kept = self._polls.get(machine_id)
                    if kept is None or status.counter >= kept.counter:
                        self._polls[machine_id] = copy.deepcopy(status)
                return status
        self.watch.missing(key)
        return None

    def cached_poll(self, machine_id):
        """The newest PollStatus this store read for the machine, by any reader (the coordinator, the
        watcher, a worker); no share access. The standalone seed uses it (states.md §6.12 item 3)."""
        with self._lock:
            return copy.deepcopy(self._polls.get(machine_id))

    def write_poll(self, status):
        _check_id(status.machine_id, "machine id")
        _check_id(status.printer_id, "printer id")
        mid = status.machine_id
        with self._lock:
            floor = self._max_poll.get(mid)
        if floor is not None and status.counter <= floor:
            raise ValueError(f"poll counter {status.counter} for {mid} doesn't go up (seen {floor})")
        written = dataclasses.replace(status, poller=self.identity.key, written_at=self.clock.time())
        self.write_json(f"status/machines/{mid}.poll.json", {"file_type": "poll", **to_json(written)})
        self._note_poll_counter(mid, written.counter)
        self.watch.see("poll:" + mid, written.counter)

    def poll_age(self, machine_id):
        return self.watch.age("poll:" + machine_id)

    def check_poller_paused(self, machine_id, token):
        """"paused" | "stale" | "missing" | "polling" (storage.md §11.6)."""
        _check_id(machine_id, "machine id")
        rel, key = f"status/machines/{machine_id}.poll.json", "poll:" + machine_id
        kind, d = self._read_status(rel, "poll")
        stable = None
        if kind == "newer":
            stable = d[0]
        elif kind == "ok":
            try:
                status = from_json(PollStatus, d)
                if status.machine_id == machine_id:
                    stable = d
            except ValueError:
                pass
        if stable is None or not _is_int(stable.get("counter")):
            gone = self.watch.missing(key)
            return "missing" if gone >= self.timings.stale_s else "polling"
        counter = stable["counter"]
        self._note_poll_counter(machine_id, counter)
        age = self.watch.see(key, counter)
        if (token and stable.get("paused_for_lock") == token) or stable.get("poller") == self.identity.key:
            return "paused"
        if age >= self.timings.stale_s:
            return "stale"
        return "polling"

    # status: expected state and line status

    def _counted_read(self, rel, file_type, cls, key, id_field, record_id):
        """(kind, object or None) of a counted status file, with the watch updated."""
        kind, d = self._read_status(rel, file_type)
        if kind == "newer":
            stable, err = d
            if _is_int(stable.get("counter")):
                self.watch.see(key, stable["counter"])
            else:
                self.watch.missing(key)
            return "newer", err
        if kind == "ok":
            try:
                obj = from_json(cls, d)
                if getattr(obj, id_field) != record_id:
                    raise ValueError("id differs from the file name")
            except ValueError:
                pass
            else:
                self.watch.see(key, obj.counter)
                return "ok", obj
        self.watch.missing(key)
        return "missing", None

    def read_expected(self, machine_id):
        _check_id(machine_id, "machine id")
        kind, obj = self._counted_read(f"status/machines/{machine_id}.expected.json", "expected", ExpectedState,
                                       "expected:" + machine_id, "machine_id", machine_id)
        if kind == "newer":
            with self._lock:
                self._expected[machine_id] = None
            raise obj
        with self._lock:
            self._expected[machine_id] = copy.deepcopy(obj)
        return obj

    def write_expected(self, state):
        mid = state.machine_id
        _check_id(mid, "machine id")
        rel = f"status/machines/{mid}.expected.json"
        kind, obj = self._counted_read(rel, "expected", ExpectedState, "expected:" + mid, "machine_id", mid)
        if kind == "newer":
            raise obj
        written = dataclasses.replace(state, counter=(obj.counter if obj else 0) + 1, written_at=self.clock.time(),
                                      written_by=self.identity.user, computer=self.identity.computer)
        self.write_json(rel, {"file_type": "expected", **to_json(written)})
        with self._lock:
            self._expected[mid] = copy.deepcopy(written)
        self.watch.see("expected:" + mid, written.counter)

    def cached_expected(self, machine_id):
        """The last ExpectedState this store read or wrote for the machine; no share access."""
        with self._lock:
            return copy.deepcopy(self._expected.get(machine_id))

    def read_line_status(self, line_id):
        _check_id(line_id, "line id")
        kind, obj = self._counted_read(f"status/lines/{line_id}.json", "line_status", LineStatus,
                                       "line:" + line_id, "line_id", line_id)
        if kind == "newer":
            raise obj
        return obj

    def write_line_status(self, status):
        lid = status.line_id
        _check_id(lid, "line id")
        rel = f"status/lines/{lid}.json"
        kind, obj = self._counted_read(rel, "line_status", LineStatus, "line:" + lid, "line_id", lid)
        if kind == "newer":
            raise obj
        written = dataclasses.replace(status, counter=(obj.counter if obj else 0) + 1, written_at=self.clock.time(),
                                      written_by=self.identity.user)
        self.write_json(rel, {"file_type": "line_status", **to_json(written)})
        self.watch.see("line:" + lid, written.counter)

    # status: acks and pictures

    def read_ack(self, alert_id):
        """Opened by name, never by listing. A found ack is kept for the life of the store."""
        if not isinstance(alert_id, str) or not _ALERT_RE.match(alert_id):
            raise ValueError(f"bad alert id {alert_id!r}")
        with self._lock:
            if alert_id in self._acks:
                return copy.deepcopy(self._acks[alert_id])
        kind, d = self._read_status(f"status/alerts/{alert_id}.json", "ack")
        ack = None
        if kind == "ok":
            try:
                ack = from_json(Ack, d)
            except ValueError:
                ack = None
        elif kind == "newer":
            # An ack from a newer Stripes still means acknowledged (alert_id is a stable key)
            stable = d[0]
            if stable.get("alert_id") == alert_id:
                at = _finite(stable.get("at"))
                ack = Ack(alert_id=alert_id, by=stable.get("by") if isinstance(stable.get("by"), str) else "",
                          computer=stable.get("computer") if isinstance(stable.get("computer"), str) else "",
                          at=at if at is not None else 0.0)
        if ack is None or ack.alert_id != alert_id:
            return None
        with self._lock:
            self._acks[alert_id] = copy.deepcopy(ack)
        return ack

    def write_ack(self, ack):
        """Created once; if the file already exists the first ack stays and this does nothing."""
        if not isinstance(ack.alert_id, str) or not _ALERT_RE.match(ack.alert_id):
            raise ValueError(f"bad alert id {ack.alert_id!r}")
        if self.create_once(f"status/alerts/{ack.alert_id}.json", encode_json("ack", to_json(ack))):
            with self._lock:
                self._acks[ack.alert_id] = copy.deepcopy(ack)

    def read_alarm_marker(self, machine_id):
        """The machine's NOT MONITORED alarm marker (§5.9), or None when missing or damaged. Read by name,
        never cached: it changes with each occurrence."""
        _check_id(machine_id, "machine id")
        kind, d = self._read_status(f"status/machines/{machine_id}.nm-alarm.json", "nm_alarm")
        if kind == "newer":     # machine_id and alarm_id are stable keys (§3.4)
            d = {k: d[0].get(k) for k in ("machine_id", "alarm_id")}
        elif kind != "ok":
            return None
        try:
            marker = from_json(AlarmMarker, d)
        except ValueError:
            return None
        if marker.machine_id != machine_id or not _ALERT_RE.match(marker.alarm_id):
            return None
        return marker

    def write_alarm_marker(self, marker):
        """Written whole over any earlier one (a new occurrence), with by and computer set to this PC's."""
        _check_id(marker.machine_id, "machine id")
        if not isinstance(marker.alarm_id, str) or not _ALERT_RE.match(marker.alarm_id):
            raise ValueError(f"bad alert id {marker.alarm_id!r}")
        written = dataclasses.replace(marker, by=self.identity.user, computer=self.identity.computer)
        self.write_json(f"status/machines/{marker.machine_id}.nm-alarm.json",
                        {"file_type": "nm_alarm", **to_json(written)})

    def read_preview(self, machine_id):
        _check_id(machine_id, "machine id")
        raw = self.read_bytes(f"status/machines/{machine_id}.preview.bmp")
        if raw is None or len(raw) > STATUS_MAX_BYTES:
            return None
        return raw

    def write_preview(self, machine_id, bmp):
        _check_id(machine_id, "machine id")
        if len(bmp) > STATUS_MAX_BYTES:
            raise ValueError(f"preview too big ({len(bmp)} bytes)")
        self.write_bytes(f"status/machines/{machine_id}.preview.bmp", bmp)

    # locks

    def _lock_rel(self, printer_id):
        return f"status/locks/{printer_id}.lock"

    def _parse_lock(self, printer_id, raw):
        """The LockInfo in a lock file's bytes: valid, from a newer format, or damaged (storage.md §11.7)."""
        rel = self._lock_rel(printer_id)
        try:
            if len(raw) > STATUS_MAX_BYTES:
                raise ValueError("too big")
            info = from_json(LockInfo, decode_json(raw, file_type="lock", rel=rel))
            if info.printer_id != printer_id or not info.token:
                raise ValueError("not this printer's lock")
            return info
        except FormatTooNew:
            stable = _lenient_json(raw) or {}
            token, counter = stable.get("token"), stable.get("counter")
            if isinstance(token, str) and token and _is_int(counter):
                def text(key):
                    value = stable.get(key)
                    return value if isinstance(value, str) else ""
                return LockInfo(printer_id=printer_id, token=token, user=text("user"), computer=text("computer"),
                                pid=0, purpose=text("purpose") or "unknown", counter=counter)
        except ValueError:
            pass
        crc = zlib.crc32(bytes(raw)) & 0xFFFFFFFF
        return LockInfo(printer_id=printer_id, token=f"damaged-{crc:08x}", user="", computer="", pid=0,
                        purpose="unknown", counter=crc)

    def read_lock(self, printer_id):
        """The printer's lock, opened by name; None when there is none. Updates the watch."""
        _check_id(printer_id, "printer id")
        raw = self.read_bytes(self._lock_rel(printer_id))
        info = self._parse_lock(printer_id, raw) if raw is not None else None
        if info is not None:
            with self._lock:
                if self._released_live(info.token):
                    info = None         # ours, released; its removal is still pending
        if info is None:
            self.watch.missing(f"lock:{printer_id}")
            with self._lock:
                self._lock_tokens[printer_id] = None
            return None
        self.watch.see(f"lock:{printer_id}:{info.token}", info.counter)
        with self._lock:
            self._lock_tokens[printer_id] = info.token
        return info

    def lock_age(self, printer_id):
        with self._lock:
            token = self._lock_tokens.get(printer_id)
        return self.watch.age(f"lock:{printer_id}:{token}") if token else None

    def lock(self, printer_id, *, purpose, run_id, line_id="", machine_id=""):
        _check_id(printer_id, "printer id")
        if purpose not in LOCK_PURPOSES:
            raise ValueError(f"unknown lock purpose {purpose!r}")
        for value, what in ((line_id, "line id"), (machine_id, "machine id")):
            if value:
                _check_id(value, what)
        return PrinterLock(self, printer_id, purpose=purpose, run_id=run_id, line_id=line_id, machine_id=machine_id)

    def local_lock_info(self, printer_id):
        """A copy of the LockInfo of this process's lock on the printer (with its latest note), or None."""
        with self._lock:
            if printer_id not in self.local_locks:
                return None
            return copy.deepcopy(self._local_lock_infos.get(printer_id))

    def _note_released(self, printer_id, token, until, *, pending):
        """until: the token's last_ok + lock_stale_s / 2. Before it no other PC can take the lock over, so
        a read-then-remove of its file is safe; after it the token is forgotten (storage.md §11.5)."""
        with self._lock:
            self._released[token] = (printer_id, until)
            while len(self._released) > _RELEASED_KEEP:
                self._released.popitem(last=False)
            if pending:
                self._pending_releases[token] = (printer_id, until)

    def _released_live(self, token):
        """Lock held. True while token is one this store released and its file may still be removed;
        once that time has passed the token is forgotten, and its file is an ordinary lock (§11.5)."""
        entry = self._released.get(token)
        if entry is None:
            return False
        if self.clock.monotonic() < entry[1]:
            return True
        self._released.pop(token, None)
        self._pending_releases.pop(token, None)
        return False

    def _remove_released(self, rel, printer_id, raw):
        """Removes the lock file read as raw when it holds a released token that may still be removed."""
        token = self._parse_lock(printer_id, raw).token
        with self._lock:
            if not self._released_live(token):
                return
        self.remove(rel)
        with self._lock:
            self._pending_releases.pop(token, None)

    def _printer_mutex(self, printer_id):
        with self._lock:
            return self._printer_mutexes.setdefault(printer_id, threading.Lock())

    def _retry_releases(self):
        with self._lock:
            pending = list(self._pending_releases.items())
        for token, (printer_id, _until) in pending:
            mutex = self._printer_mutex(printer_id)
            if not mutex.acquire(blocking=False):
                continue                # this PC's acquire() on that printer is running: it clears the file itself
            try:
                with self._lock:
                    if not self._released_live(token):
                        continue        # too late to remove it safely: forgotten
                rel = self._lock_rel(printer_id)
                raw = self.read_bytes(rel)
                if raw is not None and self._parse_lock(printer_id, raw).token == token:
                    self._remove_released(rel, printer_id, raw)
                with self._lock:
                    self._pending_releases.pop(token, None)
            finally:
                mutex.release()

    def _remove_if_released(self, printer_id):
        """Removes the printer's lock file if it holds a token this store released, while that is safe."""
        rel = self._lock_rel(printer_id)
        raw = self.read_bytes(rel)
        if raw is not None:
            self._remove_released(rel, printer_id, raw)

    # heartbeats

    def heartbeat(self):
        return self._heartbeat

    def live_heartbeats(self):
        """The heartbeats whose counter moved within stale_s, of this exact format, sorted by key."""
        found = {}
        own_key = self.identity.key
        for name in self.listdir("status/pollers"):
            m = _HB_NAME_RE.match(name)
            if not m or m.group(1) == own_key:
                continue
            key, rel = m.group(1), f"status/pollers/{name}"
            raw = self.read_bytes(rel)
            if raw is None or len(raw) > STATUS_MAX_BYTES:
                continue
            loose = _lenient_json(raw)
            if not loose or not _is_int(loose.get("format_version")) or loose["format_version"] != FORMAT:
                continue
            try:
                hb = from_json(HeartbeatInfo, decode_json(raw, file_type="heartbeat", rel=rel))
            except (ValueError, FormatTooNew):
                continue
            if hb.key != key:
                continue
            age = self.watch.see("hb:" + key, hb.counter)
            if age >= 40 * self.timings.stale_s:
                # Left by a PC that stopped long ago: anyone may tidy it up
                try:
                    self.remove(rel)
                except ShareUnavailable:
                    raise
                except ShareError:
                    pass
                self.watch.forget("hb:" + key)
                continue
            if age < self.timings.stale_s:
                found[key] = hb
        own = self._heartbeat.last
        if own is not None:
            found[own.key] = copy.deepcopy(own)    # a listing may hide our own new file for 10 s
        return [found[k] for k in sorted(found)]

    # logs

    def put_log_bytes(self, kind, name, raw):
        """Writes a log file once under its final name; returns the name it landed under."""
        if kind not in LOG_KINDS:
            raise ValueError(f"unknown log kind {kind!r}")
        m = LOG_NAME_RE.match(name)
        if not m or m.group(5):
            raise ValueError(f"bad log name {name!r}")
        folder = f"logs/{kind}/{log_month(name)}"
        stem = name[:-len(".json")]
        for candidate in [name] + [f"{stem}-{n}.json" for n in range(2, 10)]:
            rel = f"{folder}/{candidate}"
            if self._create_log(folder, rel, raw):
                return candidate
            # Already there: the same bytes mean it was uploaded before but its marker wasn't written;
            # anything else (an unfinished file) means the next name
            if self.read_bytes(rel) == raw:
                return candidate
        raise ShareError(Msg(N_("Stripes couldn't use {folder} on the server: {error}"),
                             {"folder": folder, "error": "every name for this entry is taken"}))

    def _create_log(self, folder, rel, raw):
        path = self._path(rel)
        try:
            return self._call(self.fs.create_exclusive, path, raw)
        except FileNotFoundError:
            pass
        except OSError as e:
            raise self._mapped(rel, e) from None
        self.makedirs(folder)           # the month folder is new
        return self._write_op(rel, self.fs.create_exclusive, path, raw)

    def append_log(self, kind, entry):
        """Validates and writes one entry; returns the file name. Raises on any failure, never queues
        (application code logs through DataService.append_log)."""
        at = self.clock.time()
        stored = make_log_record(kind, entry, self.identity, at)
        return self.put_log_bytes(kind, log_name(entry, at), encode_log(stored))

    def list_logs(self, kind, *, since=None, entry_type="", ref=""):
        """Log names, sorted, filtered by name only (no file is opened)."""
        if kind not in LOG_KINDS:
            raise ValueError(f"unknown log kind {kind!r}")
        base = f"logs/{kind}"
        if since is None:
            months = [m for m in self.listdir(base) if _MONTH_RE.match(m)]
            since_stamp = ""
        else:
            since_stamp = _stamp(since)
            months = _months(since_stamp, _stamp(self.clock.time()))
        names = []
        for month in sorted(months):
            for name in self.listdir(f"{base}/{month}"):
                m = LOG_NAME_RE.match(name)
                if not m or log_month(name) != month:
                    continue
                if since_stamp and m.group(1) < since_stamp:
                    continue
                if (entry_type and m.group(2) != entry_type) or (ref and m.group(3) != ref):
                    continue
                names.append(name)
        return sorted(names)

    def read_log(self, kind, name):
        """The stored entry with "log_name" added; None if missing, unfinished, damaged or newer."""
        if kind not in LOG_KINDS or not LOG_NAME_RE.match(name):
            raise ValueError(f"bad log {kind}/{name}")
        raw = self.read_bytes(f"logs/{kind}/{log_month(name)}/{name}")
        if raw is None or len(raw) > LOG_ENTRY_MAX_BYTES:
            return None
        try:
            stored = decode_log(raw)
        except FormatTooNew:
            return None
        if stored is None:
            return None
        stored["log_name"] = name
        return stored

    def read_logs(self, kind, *, since=None):
        """Every complete entry, oldest first."""
        out = []
        for name in self.list_logs(kind, since=since):
            entry = self.read_log(kind, name)
            if entry is not None:
                out.append(entry)
        return out


def _months(since_stamp, now_stamp):
    """Month folders from since's month up to the current month + 1 (another PC's clock may be ahead)."""
    y, m = int(since_stamp[:4]), int(since_stamp[4:6])
    end_y, end_m = int(now_stamp[:4]), int(now_stamp[4:6]) + 1
    if end_m > 12:
        end_y, end_m = end_y + 1, 1
    months = [f"{y:04d}-{m:02d}"]
    while (y, m) < (end_y, end_m):
        m += 1
        if m > 12:
            y, m = y + 1, 1
        months.append(f"{y:04d}-{m:02d}")
    return months


# --------------------------------------------------------------------------
# Printer locks
# --------------------------------------------------------------------------

class PrinterLock:
    """Thread-safe: acquire() and start_keeping() on one thread; check(), update() and release() from
    any thread (a Send takes the locks on its whole-Send thread and its machine threads use them).
    One internal lock guards the state; no share operation runs while holding it."""

    def __init__(self, store, printer_id, *, purpose, run_id, line_id="", machine_id=""):
        self.store = store
        self.printer_id = printer_id
        self.purpose = purpose
        self.run_id = run_id
        self.line_id = line_id
        self.machine_id = machine_id
        self.info = None
        self.state = "unheld"
        self._lock = threading.Lock()
        self._release_lock = threading.Lock()
        self._last_ok = 0.0
        self._note = {}             # update() notes not yet written
        self._keeper = None
        self._stop = None

    @property
    def token(self):
        with self._lock:
            return self.info.token if self.state == "held" and self.info else ""

    def _rel(self):
        return self.store._lock_rel(self.printer_id)

    def acquire(self):
        """"held", or "local_only" when the share is unreachable. Raises LockHeld for a live holder."""
        store = self.store
        with store._lock:
            if self.printer_id in store.local_locks:
                holder = copy.deepcopy(store._local_lock_infos.get(self.printer_id)) or _unknown_holder(self.printer_id)
                raise LockHeld(_held_msg(holder), info=holder)
            info = LockInfo(printer_id=self.printer_id, token=secrets.token_hex(8), user=store.identity.user,
                            computer=store.identity.computer, pid=store.identity.pid, purpose=self.purpose,
                            line_id=self.line_id, machine_id=self.machine_id, run_id=self.run_id, counter=1,
                            acquired_at=store.clock.time(), note={})
            # Before any share access, so this PC's poller steps aside at once
            store.local_locks[self.printer_id] = self.run_id
            store._local_lock_infos[self.printer_id] = copy.deepcopy(info)
        with self._lock:
            self.info = info
            self.state = "unheld"
            self._note = {}
        try:
            # One real try even while the share is marked down: a single slow operation marks it down
            # for share_retry_s, and a local_only lock lets other PCs poll during the Send. At most
            # share_timeout_s, so a real outage still falls back at once (storage.md §11.2 step 4)
            store.reach_now()
            # Waits for a pending release's read-then-remove on this printer to finish, so it never
            # removes the file made here (storage.md §11.5)
            with store._printer_mutex(self.printer_id):
                return self._acquire_on_share(info)
        except ShareUnavailable:
            with self._lock:
                self.state = "local_only"
            return "local_only"
        except BaseException:
            self._unregister()
            raise

    def _acquire_on_share(self, info):
        store, rel = self.store, self._rel()
        raw = encode_json("lock", to_json(info))
        last_seen = None
        for _ in range(3):
            # last_ok is the time just before the create (storage.md §11.2 step 4): other PCs can't see
            # the file earlier, so a PC frozen after it landed wakes with an expired lease, never a fresh one
            t0 = store.clock.monotonic()
            if store.create_once(rel, raw):
                return self._held(t0)
            current = store.read_lock(self.printer_id)
            if current is None:
                # Gone meanwhile, or a lock of ours whose removal is pending: clear that and try again
                store._remove_if_released(self.printer_id)
                continue
            last_seen = current
            age = store.lock_age(self.printer_id) or 0.0
            if age < store.timings.lock_stale_s:
                raise LockHeld(_held_msg(current), info=current)
            t0 = self._take_over(current, age, raw)
            if t0 is not None:
                return self._held(t0)
        holder = last_seen or _unknown_holder(self.printer_id)
        raise LockHeld(_held_msg(holder), info=holder)

    def _take_over(self, stale, age, raw):
        """storage.md §11.4. The clock.monotonic() taken just before the create that made our file when
        held; None when the file moved meanwhile (try again)."""
        store, rel = self.store, self._rel()
        aside = f"{rel}.stale-{self.info.token}"
        if not store._rename(rel, aside):
            return None
        moved_raw = store.read_bytes(aside)
        moved = store._parse_lock(self.printer_id, moved_raw) if moved_raw is not None else None
        if moved is None or moved.token != stale.token:
            # Someone else's new lock went aside: put it back
            try:
                store._rename(aside, rel)
            except ShareError:
                pass
            holder = moved or stale
            raise LockHeld(_held_msg(holder), info=holder)
        t0 = store.clock.monotonic()
        if not store.create_once(rel, raw):
            try:
                store.remove(aside)
            except ShareError:
                pass
            try:
                holder = store.read_lock(self.printer_id) or _unknown_holder(self.printer_id)
            except ShareError:
                holder = _unknown_holder(self.printer_id)
            raise LockHeld(_held_msg(holder), info=holder)
        try:
            store.remove(aside)
        except ShareError:
            pass
        try:
            store.append_log("hx-events", {
                "type": "lock_taken_over", "printer_id": self.printer_id,
                "previous": {"token": stale.token, "user": stale.user, "computer": stale.computer,
                             "purpose": stale.purpose, "run_id": stale.run_id, "counter": stale.counter},
                "unchanged_s": round(age, 1)})
        except (ShareError, ValueError) as e:
            print(f"Could not log the lock takeover of {self.printer_id}: {e}", file=sys.stderr)
        return t0

    def _held(self, t0):
        # t0: taken just before the create that made the file, never after the share calls that follow
        # it (the takeover's remove and log), so a slow or frozen one can only shorten the lease
        with self._lock:
            self.state = "held"
            self._last_ok = t0
        return "held"

    def start_keeping(self):
        """Starts 'hx-lock-<printer>', which bumps the lock's counter every lock_refresh_s."""
        with self._lock:
            if self.state != "held" or self._keeper is not None:
                return
            self._stop = CancelToken()
            self._keeper = threading.Thread(target=self._keep, args=(self._stop,),
                                            name=f"hx-lock-{self.printer_id}", daemon=True)
            keeper = self._keeper
        keeper.start()

    def _keep(self, stop):
        try:
            while self.store.clock.sleep(self.store.timings.lock_refresh_s, stop):
                if not self._keep_once():
                    return
        except Exception:
            traceback.print_exc()
            with self._lock:
                if self.state == "held":
                    self.state = "lost"     # the safe side: stop talking to the printer

    def _keep_once(self):
        """One refresh (storage.md §11.3). False when the keeper should stop."""
        store, rel = self.store, self._rel()
        with self._lock:
            if self.state != "held":
                return False
            info = copy.deepcopy(self.info)
            note = dict(self._note)
        try:
            raw = store.read_bytes(rel)
            if raw is None or store._parse_lock(self.printer_id, raw).token != info.token:
                with self._lock:
                    if self.state == "held":
                        self.state = "lost"
                return False
            info.counter += 1
            info.note.update(note)
            half = store.timings.lock_stale_s / 2
            with self._lock:
                # t0, the time just before the write, is what a good write proves (as the poller's p_ok):
                # other PCs can't see the new counter earlier
                t0 = store.clock.monotonic()
                if self.state == "held" and t0 - self._last_ok >= half:
                    # We stalled (PC asleep, process suspended): another PC may judge the lock stale or
                    # have taken it over, so never write again, as for a failing share below
                    self.state = "lost"
                if self.state != "held":
                    return False
            store.write_bytes(rel, encode_json("lock", to_json(info)))
        except ShareError:
            with self._lock:
                if self.state == "held" and store.clock.monotonic() - self._last_ok >= store.timings.lock_stale_s / 2:
                    # Another PC may take over soon: never write again, so we can't recreate its file
                    self.state = "lost"
                    return False
            return True
        with self._lock:
            if self.state != "held":
                return False
            if store.clock.monotonic() - t0 >= half:
                # The write came back lock_stale_s / 2 or more after it started (the PC froze inside it):
                # it proves nothing, and it may have landed over a new holder's file
                self.state = "lost"
                return False
            self.info = info
            for key, value in note.items():
                if self._note.get(key) is value:
                    del self._note[key]
            self._last_ok = t0
        return True

    def check(self, budget_s=0.0):
        """Before every printer request: LockLost when the lock is lost, or when a call lasting up to
        budget_s could still run when another PC may judge the lock stale."""
        stale = self.store.timings.lock_stale_s
        with self._lock:
            if self.state == "held" and self.store.clock.monotonic() - self._last_ok + budget_s >= stale / 2:
                self.state = "lost"
            if self.state == "lost":
                raise LockLost(Msg(N_("Another PC took over this printer's lock.")))

    def lease_end(self):
        """The clock.monotonic() time by which every printer call made under this lock must have ended:
        last_ok + lock_stale_s / 2, the bound check() uses (storage.md §11.3). Holders pass it as the
        deadline of each call, so a call that passed its check() and then waited (the gate, a frozen PC)
        is refused instead of sent late. None when the lock isn't held on the share (local_only, unheld).
        LockLost when it is lost."""
        with self._lock:
            if self.state == "lost":
                raise LockLost(Msg(N_("Another PC took over this printer's lock.")))
            if self.state != "held":
                return None
            return self._last_ok + self.store.timings.lock_stale_s / 2

    def update(self, **note):
        """e.g. update(step=12, of=40); written at the next refresh."""
        json.dumps(note, allow_nan=False)       # a note that can't be stored is a programming error
        with self._lock:
            self._note.update(note)
            token = self.info.token if self.info else None
        store = self.store
        with store._lock:
            mine = store._local_lock_infos.get(self.printer_id)
            if mine is not None and mine.token == token:
                mine.note.update(copy.deepcopy(note))

    def release(self):
        """Idempotent: stops the keeper, removes the file if it is still ours, and the local entry."""
        with self._release_lock:
            with self._lock:
                keeper, stop = self._keeper, self._stop
                self._keeper = self._stop = None
            if stop is not None:
                stop.cancel()
            if keeper is not None and keeper is not threading.current_thread():
                keeper.join(self.store.timings.lock_refresh_s)
            with self._lock:
                until = self._last_ok + self.store.timings.lock_stale_s / 2
                if self.state == "held" and self.store.clock.monotonic() >= until:
                    # Stalled past the lease: another PC may take the file over any moment, so a
                    # read-then-remove could delete its new lock. Leave it to go stale (§11.5)
                    self.state = "lost"
                state, info = self.state, self.info
            if state == "held" and info is not None:
                self._remove_file(info.token, until)
            self._unregister()
            with self._lock:
                self.state = "unheld"

    def _remove_file(self, token, until):
        """Removes our file, only while monotonic() < until (last_ok + lock_stale_s / 2): before then no
        other PC can have taken it over, so the file read is still the one removed."""
        store, rel = self.store, self._rel()
        try:
            raw = store.read_bytes(rel)
            if raw is not None and store._parse_lock(self.printer_id, raw).token == token \
                    and store.clock.monotonic() < until:
                store.remove(rel)
        except ShareError:
            store._note_released(self.printer_id, token, until, pending=True)
            return
        store._note_released(self.printer_id, token, until, pending=False)

    def _unregister(self):
        store = self.store
        with self._lock:
            token = self.info.token if self.info else None
        with store._lock:
            mine = store._local_lock_infos.get(self.printer_id)
            if token is not None and mine is not None and mine.token == token:
                store._local_lock_infos.pop(self.printer_id, None)
                store.local_locks.pop(self.printer_id, None)

    def __enter__(self):
        self.acquire()
        self.start_keeping()
        return self

    def __exit__(self, *exc):
        self.release()


# --------------------------------------------------------------------------
# Heartbeats and reach memory
# --------------------------------------------------------------------------

class Heartbeat:
    """This process's heartbeat file in status/pollers."""

    def __init__(self, store):
        self.store = store
        self.key = store.identity.key
        self.last = None            # the last HeartbeatInfo written (or tried)
        self._counter = 0
        self._lock = threading.Lock()

    def beat(self, *, can_poll, last_printer_reach, polling, monitor_open, floor_board, declined=()):
        if not _KEY_RE.match(self.key):
            raise ValueError(f"bad heartbeat key {self.key!r}")
        store = self.store
        with self._lock:
            self._counter += 1
            info = HeartbeatInfo(key=self.key, computer=store.identity.computer, user=store.identity.user,
                                 pid=store.identity.pid, version=store.version, counter=self._counter,
                                 written_at=store.clock.time(), can_poll=bool(can_poll),
                                 last_printer_reach=last_printer_reach, polling=list(polling),
                                 monitor_open=bool(monitor_open), floor_board=bool(floor_board),
                                 declined=sorted(declined))
            self.last = info
        store.write_json(f"status/pollers/{self.key}.json", {"file_type": "heartbeat", **to_json(info)})
        store.watch.see("hb:" + self.key, info.counter)

    def remove(self):
        """On a clean exit; best effort."""
        with self._lock:
            self.last = None
        try:
            self.store.remove(f"status/pollers/{self.key}.json")
        except (ShareError, ValueError):
            pass


class ReachMemory:
    """When this user's Stripes last reached a printer, kept in <local>/hx/reach.json so it survives
    restarts. A PC is given printers only if it reached one within printer_reach_window_s."""

    def __init__(self, local, *, clock: Clock = SYSTEM_CLOCK, timings: Timings = TIMINGS):
        self.local = local
        self.clock = clock
        self.timings = timings
        self._lock = threading.Lock()
        self._last_probe = None
        stored = None
        if local is not None:
            try:
                value = local.read_local_json("reach.json").get("last_printer_reach")
                stored = _finite(value)
            except OSError:
                stored = None
        self.last_printer_reach = stored
        self._written = stored

    def reached(self):
        """After every successful printer reply and every successful probe."""
        now = self.clock.time()
        with self._lock:
            self.last_printer_reach = now
            # Also after a clock set back, so the stored value never stays in the future
            write = self._written is None or abs(now - self._written) >= _REACH_WRITE_S
            if write:
                self._written = now
        if write and self.local is not None:
            try:
                self.local.write_local_json("reach.json", {"last_printer_reach": now})
            except OSError as e:
                print(f"Could not save reach.json: {e}", file=sys.stderr)

    def can_poll(self):
        with self._lock:
            last = self.last_printer_reach
        if last is None:
            return False
        age = self.clock.time() - last
        return -CLOCK_BACK_TOLERANCE_S <= age <= self.timings.printer_reach_window_s

    def probe_due(self, *, unpolled):
        """A probe only when some active printer has no live poller, at most every 4 x stale_s."""
        if not unpolled:
            return False
        with self._lock:
            last = self._last_probe
        return last is None or self.clock.monotonic() - last >= 4 * self.timings.stale_s

    def probed(self, ok):
        with self._lock:
            self._last_probe = self.clock.monotonic()


# --------------------------------------------------------------------------
# LocalStore: this user's and this PC's folders
# --------------------------------------------------------------------------

_DATA_PARTS = (("lines", Line), ("machines", Machine), ("printers", Printer),
               ("adjustments", MachineFilmAdjustment), ("calibrations", Calibration), ("products", Product),
               ("films", Film), ("print_texts", PrintText))


def _same_root(a, b):
    if a is None or b is None:
        return a is None and b is None
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


class LocalStore:
    """This user's and this PC's v2 folders: settings, local copy, previews, send-log queue.
    Local disk only (no hx-io timeout). Thread-safe through one lock."""

    def __init__(self, paths: HxPaths, *, clock: Clock = SYSTEM_CLOCK, identity: Identity | None = None):
        self.paths = paths
        self.clock = clock
        self.identity = identity        # for upload markers; DataService fills it in
        self.fs = FileOps()
        self.fs.tmp_tag = f"local-{os.getpid()}"
        self.fs.create_retry_s = ()     # local names are unique: a refused create is a real refusal
        self._lock = threading.RLock()
        self.hx_dir = os.path.join(paths.local, "hx")
        self.preview_dir = os.path.join(self.hx_dir, "previews")
        self.queue_dir = os.path.join(paths.machine, "send-queue")
        self.fallback_queue_dir = os.path.join(self.hx_dir, "send-queue")

    def _write(self, folder, name, raw):
        with self._lock:
            self.fs.makedirs(folder)
            self.fs.write_whole(os.path.join(folder, name), raw)

    def _read_dict(self, path, file_type, rel):
        try:
            raw = self.fs.read_bytes(path)
            return decode_json(raw, file_type=file_type, rel=rel)
        except (OSError, ValueError, FormatTooNew):
            return None

    @staticmethod
    def _check_name(name):
        if not isinstance(name, str) or not _LOCAL_NAME_RE.match(name):
            raise ValueError(f"bad local file name {name!r}")

    # settings and small state

    def read_user_json(self, name):
        """<roaming>/<name> without its envelope; {} if missing, damaged or newer."""
        self._check_name(name)
        d = self._read_dict(os.path.join(self.paths.roaming, name), "user_settings", name)
        return {k: v for k, v in d.items() if k not in ENVELOPE_KEYS} if d else {}

    def write_user_json(self, name, data):
        self._check_name(name)
        self._write(self.paths.roaming, name, encode_json("user_settings", data))

    def read_local_json(self, name):
        """<local>/hx/<name>; {} if missing, damaged or newer (only reach.json uses it)."""
        self._check_name(name)
        d = self._read_dict(os.path.join(self.hx_dir, name), "local_state", name)
        return {k: v for k, v in d.items() if k not in ENVELOPE_KEYS} if d else {}

    def write_local_json(self, name, data):
        self._check_name(name)
        self._write(self.hx_dir, name, encode_json("local_state", data))

    # the local copy

    def read_local_copy(self):
        """The local copy as HxData (source "local"), or None if missing, damaged, newer or from
        another share."""
        d = self._read_dict(os.path.join(self.hx_dir, "local-copy.json"), "local_copy", "local-copy.json")
        if d is None or not _same_root(d.get("share_root"), self.paths.share_root):
            return None
        try:
            parts = {}
            for name, cls in _DATA_PARTS:
                group = d.get(name, {})
                if not isinstance(group, dict):
                    raise ValueError(f"{name}: not an object")
                records = {}
                for key, value in group.items():
                    rec = from_json(cls, value)
                    if rec.id != key:
                        raise ValueError(f"{name}: {key} holds {rec.id}")
                    records[key] = rec
                parts[name] = records
            company = from_json(Company, d["company"])
            reached = d.get("reached_at")
            if reached is not None:
                reached = _finite(reached)
                if reached is None:
                    raise ValueError("reached_at")
        except (KeyError, ValueError, TypeError):
            return None
        return HxData(company=company, **parts, source="local", loaded_at=self.clock.time(),
                      reached_at=reached)

    def write_local_copy(self, data, *, changes_counter=None):
        """One consistent set, written whole then swapped in: every record, but only active films
        and print texts."""
        body = {"written_at": self.clock.time(), "share_root": self.paths.share_root,
                "reached_at": data.reached_at, "loaded_at": data.loaded_at,
                "changes_counter": changes_counter, "company": to_json(data.company)}
        for name, _cls in _DATA_PARTS:
            records = getattr(data, name)
            if name in ("films", "print_texts"):
                records = {k: v for k, v in records.items() if v.status == "active"}
            body[name] = {k: to_json(v) for k, v in records.items()}
        self._write(self.hx_dir, "local-copy.json", encode_json("local_copy", body))

    # photo previews

    def preview_path(self, sha256):
        if not isinstance(sha256, str) or not _SHA_RE.match(sha256):
            raise ValueError(f"bad sha256 {sha256!r}")
        return os.path.join(self.preview_dir, f"{sha256}.png")

    def has_preview(self, sha256):
        with self._lock:
            return self.fs.exists(self.preview_path(sha256))

    def write_preview(self, sha256, png):
        path = self.preview_path(sha256)
        self._write(self.preview_dir, os.path.basename(path), png)

    def prune_previews(self, keep):
        """Deletes every cached preview whose sha256 isn't in keep."""
        with self._lock:
            try:
                names = self.fs.listdir(self.preview_dir)
            except FileNotFoundError:
                return
            for name in names:
                if name.endswith(".png") and _SHA_RE.match(name[:-4]) and name[:-4] not in keep:
                    try:
                        self.fs.remove(os.path.join(self.preview_dir, name))
                    except OSError:
                        pass

    # the upload queue

    def queue_log(self, kind, name, stored):
        """One entry waiting to upload: the PC's queue, or this user's own when that can't be written."""
        if kind not in LOG_KINDS or not isinstance(name, str) or not LOG_NAME_RE.match(name):
            raise ValueError(f"bad log {kind}/{name}")
        raw = encode_log(stored)
        file_name = f"{kind}~{name}"
        with self._lock:
            try:
                self._queue_in(self.queue_dir, file_name, raw)
            except PermissionError:
                # %PROGRAMDATA% may not let this user create files: keep it in the user's own folder
                self._queue_in(self.fallback_queue_dir, file_name, raw)

    def _queue_in(self, folder, file_name, raw):
        self.fs.makedirs(folder)
        path = os.path.join(folder, file_name)
        if not self.fs.create_exclusive(path, raw):
            if self.fs.read_bytes(path) != raw:
                raise FileExistsError(errno.EEXIST, "another entry has this name", path)

    def _queue_files(self):
        """(folder, file name, kind, log name, marked) of every queued entry."""
        out = []
        for folder in (self.queue_dir, self.fallback_queue_dir):
            try:
                names = self.fs.listdir(folder)
            except OSError:
                continue
            have = set(names)
            for file_name in sorted(names):
                kind, sep, name = file_name.partition("~")
                if not sep or kind not in LOG_KINDS or not LOG_NAME_RE.match(name):
                    continue
                out.append((folder, file_name, kind, name, file_name + ".uploaded" in have))
        return out

    def queued_logs(self):
        """(kind, name, stored) of every complete entry without an upload marker, oldest first."""
        found = {}
        with self._lock:
            for folder, file_name, kind, name, marked in self._queue_files():
                if marked or name in found:
                    continue
                try:
                    stored = decode_log(self.fs.read_bytes(os.path.join(folder, file_name)))
                except (OSError, FormatTooNew):
                    continue
                if stored is not None:
                    found[name] = (kind, name, stored)
        return [found[k] for k in sorted(found)]

    def queue_send_log(self, name, entry):
        self.queue_log("hx-sends", name, entry)

    def pending_send_logs(self):
        return [(name, stored) for kind, name, stored in self.queued_logs() if kind == "hx-sends"]

    def mark_uploaded(self, name):
        """Marks the queued entry with this log name as uploaded (a separate file, because the uploader
        may not change another user's file), then deletes what Windows lets it delete."""
        identity = self.identity or hx_win.current_identity()
        with self._lock:
            for folder, file_name, _kind, log, marked in self._queue_files():
                if log != name:
                    continue
                entry = os.path.join(folder, file_name)
                marker = entry + ".uploaded"
                if not marked:
                    self.fs.create_exclusive(marker, encode_json("upload_marker", {
                        "by": identity.user, "computer": identity.computer, "at": self.clock.time()}))
                try:
                    self.fs.remove(entry)
                except OSError:
                    continue            # someone else's entry: the marker stays
                try:
                    self.fs.remove(marker)
                except OSError:
                    pass


# --------------------------------------------------------------------------
# DataService
# --------------------------------------------------------------------------

_MISSING = object()


def _masked(record_type, d):
    """A record dict for the admin log: a printer token never appears in a log."""
    if d is None:
        return None
    out = {k: v for k, v in d.items() if k not in ENVELOPE_KEYS}
    if record_type == "printer" and "token" in out:
        out["token"] = "(set)" if out["token"] else ""
    return out


class DataService:
    """The current HxData. A 'hx-data' thread reloads it every data_refresh_s (or on reload()),
    refreshes the local copy after every complete load from the share, falls back to the local copy
    when the share is down, flushes the send-log queue when it can, and posts DataUpdate and
    ShareHealth on the bus."""

    def __init__(self, *, store: ShareStore, local: LocalStore, bus: EventBus, identity: Identity,
                 clock: Clock = SYSTEM_CLOCK, timings: Timings = TIMINGS):
        self.store = store
        self.local = local
        self.bus = bus
        self.identity = identity
        self.clock = clock
        self.timings = timings
        if getattr(local, "identity", None) is None:
            local.identity = identity
        self.current: HxData | None = None
        self.problems: list[Msg] = []
        self.too_new: list[str] = []
        self._lock = threading.Lock()
        self._wake = CancelToken()
        self._reload_wanted = False
        self._stopping = False
        self._thread = None
        self._last_available = None
        self._last_cycle = None      # clock.monotonic() of the last data cycle
        self._last_full = None       # ... of the last complete full reload
        self._last_counter = None    # changes.json counter of the last complete full reload
        self._again = False          # the last cycle was a full reload triggered by a change
        self._full_asked = 0         # + 1 when a save couldn't bump changes.json: load everything again
        self._full_done = 0          # the _full_asked value the last complete full reload covered
        self._local_reached_at = None   # reached_at of the local copy on disk

    # the thread

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="hx-data", daemon=True)
        self._thread.start()

    def stop(self, timeout_s=2.0):
        with self._lock:
            self._stopping = True
            self._wake.cancel()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout_s)

    def reload(self):
        """Non-blocking: a data cycle as soon as the hx-data thread can."""
        with self._lock:
            self._reload_wanted = True
            self._wake.cancel()

    def _run(self):
        try:
            copy_ = self.local.read_local_copy()
        except Exception:
            traceback.print_exc()
            copy_ = None
        if copy_ is not None and self.current is None:
            self.current = copy_
            self._local_reached_at = copy_.reached_at
            self.bus.post(DataUpdate(data=copy_))
        while True:
            with self._lock:
                if self._stopping:
                    return
                wake = self._wake
            try:
                self._pass()
            except Exception:
                traceback.print_exc()
            if not wake.cancelled:
                self.clock.sleep(self.timings.watch_s, wake)
            with self._lock:
                if self._stopping:
                    return
                if self._wake.cancelled:
                    self._wake = CancelToken()

    def _pass(self):
        store = self.store
        available = store.available
        back = False
        if available != self._last_available:
            back = available and self._last_available is False
            self._last_available = available
            self.bus.post(ShareHealth(ok=available, last_reached=store.last_reached))
            if not available and self.current is not None:
                # The content stays; a Send now says "templates as of"
                self.current = dataclasses.replace(self.current, source="local")
                self.bus.post(DataUpdate(data=self.current))
        with self._lock:
            wanted, self._reload_wanted = self._reload_wanted, False
        due = self._last_cycle is None or self.clock.monotonic() - self._last_cycle >= self.timings.data_refresh_s
        if wanted or due or back:
            self._cycle()
            self._flush()

    def run_once(self):
        """Tests: one data cycle and one flush on the calling thread."""
        self._cycle()
        self._flush()

    # loading

    def _set_problems(self, problems):
        if problems != self.problems:
            self.problems = list(problems)
            if self.current is not None:
                self.bus.post(DataUpdate(data=self.current))

    def _changes_counter(self):
        raw = self.store.read_bytes("config/hx/changes.json")
        d = _lenient_json(raw) if raw is not None else None
        if not d or d.get("file_type") != "changes" or not _is_int(d.get("counter")):
            return None
        return d["counter"]

    def _cycle(self):
        self._last_cycle = self.clock.monotonic()
        with self._lock:
            asked = self._full_asked
        if self.store.root is None:
            self._set_problems([Msg(N_("No server folder is set up for Stripes on this PC."))])
            return
        try:
            counter = self._changes_counter()
        except ShareUnavailable:
            return
        except ShareError as e:
            self._set_problems([e.msg])
            return
        current = self.current
        by_change = self._last_counter is not None and counter != self._last_counter
        full = (current is None or current.source != "share" or counter is None or counter != self._last_counter
                or self._again or asked != self._full_done
                or (self._last_full is not None
                    and self.clock.monotonic() - self._last_full >= 10 * self.timings.data_refresh_s))
        if not full:
            now = self.clock.time()
            self.current = dataclasses.replace(current, reached_at=now, loaded_at=now)
            self._again = False
            self._set_problems([])
            if self._local_reached_at is None or now - self._local_reached_at >= 600.0:
                self._save_local(self.current, previews=False)
            return
        loaded = self._load_all()
        if loaded is None:
            return                      # the share went away part-way: keep what we have
        data, problems, too_new = loaded
        if data is None:
            self.too_new = too_new
            self._again = False
            self._set_problems(problems)
            return
        self.current = data
        self.problems = []
        self.too_new = []
        self._last_counter = counter
        self._last_full = self.clock.monotonic()
        self._full_done = asked
        self._again = by_change         # a listing may have hidden new files: load again next time
        self._save_local(data, previews=True)
        self.bus.post(DataUpdate(data=data))

    def _load_all(self):
        """(HxData, [], []) when complete; (None, problems, too_new) when not; None when the share
        became unavailable."""
        store = self.store
        problems, too_new = [], []

        def damaged(rel):
            problems.append(Msg(N_("{file} on the server is damaged."), {"file": rel}))

        def read(cls, rel, rec_id):
            try:
                raw = store.read_bytes(rel)
            except ShareUnavailable:
                raise
            except ShareError as e:
                problems.append(e.msg)
                return None
            if raw is None:
                return _MISSING
            if len(raw) > RECORD_MAX_BYTES:
                damaged(rel)
                return None
            try:
                rec = from_json(cls, decode_json(raw, file_type=FILE_TYPES[cls], rel=rel))
                if rec.id != rec_id:
                    raise ValueError("id differs from the file name")
                _check_record(rec)
            except FormatTooNew as e:
                too_new.append(rel)
                problems.append(e.msg)
                return None
            except ValueError:
                damaged(rel)
                return None
            return rec

        def listing(rel):
            try:
                return store.listdir(rel)
            except ShareUnavailable:
                raise
            except ShareError as e:
                problems.append(e.msg)
                return []

        def folder(cls, rel, pattern):
            out = {}
            for name in listing(rel):
                m = pattern.match(name)
                if not m:
                    continue            # desktop.ini, Thumbs.db, *.tmp, ...
                rec = read(cls, f"{rel}/{name}", m.group(1))
                if rec is not None and rec is not _MISSING:
                    out[rec.id] = rec
            return out

        try:
            company = read(Company, "config/hx/company.json", "company")
            if company is _MISSING:
                problems.append(Msg(N_("MapleJet isn't set up on the server yet.")))
                company = None
            parts = {
                "lines": folder(Line, "config/hx/lines", _RECORD_NAME_RE),
                "machines": folder(Machine, "config/hx/machines", _RECORD_NAME_RE),
                "printers": folder(Printer, "config/hx/printers", _RECORD_NAME_RE),
                "adjustments": folder(MachineFilmAdjustment, "config/hx/adjustments", _ADJ_NAME_RE),
                "calibrations": folder(Calibration, "config/hx/calibrations", _RECORD_NAME_RE),
                "products": folder(Product, "templates/products", _RECORD_NAME_RE),
                "print_texts": folder(PrintText, "templates/print-texts", _RECORD_NAME_RE),
            }
            films = {}
            for name in listing("templates/films"):
                if not _ID_RE.match(name):
                    continue
                rec = read(Film, f"templates/films/{name}/film.json", name)
                if rec is not None and rec is not _MISSING:
                    films[rec.id] = rec
            parts["films"] = films
        except ShareUnavailable:
            return None
        if problems or company is None:
            return None, problems, too_new
        now = self.clock.time()
        return HxData(company=company, **parts, source="share", loaded_at=now, reached_at=now), [], []

    def _save_local(self, data, *, previews):
        """The local copy (and, after a full load, its photo previews). Never stops a cycle."""
        try:
            keep = None
            if previews:
                keep = self._copy_previews(data)
            self.local.write_local_copy(data, changes_counter=self._last_counter)
            self._local_reached_at = data.reached_at
            if keep is not None:
                self.local.prune_previews(keep)
        except (OSError, ValueError) as e:
            print(f"Could not save the MapleJet local copy: {e}", file=sys.stderr)

    def _copy_previews(self, data):
        """Copies the share's previews of every active film's files; returns every sha256 to keep."""
        keep, share_ok = set(), True
        for film in data.films.values():
            if film.status != "active":
                continue
            for ref in film.files:
                if not _SHA_RE.match(ref.sha256 or ""):
                    continue
                keep.add(ref.sha256)
                if not share_ok or self.local.has_preview(ref.sha256):
                    continue
                try:
                    png = self.store.read_bytes(f"templates/films/{film.id}/previews/{ref.sha256}.png")
                except ShareUnavailable:
                    share_ok = False
                    continue
                except ShareError:
                    continue
                if png and len(png) <= FILM_PREVIEW_MAX_BYTES and png.startswith(PNG_SIGNATURE):
                    self.local.write_preview(ref.sha256, png)
        return keep

    def can_send(self):
        """The 24-hour rule: (True, None) or (False, why)."""
        current = self.current
        if current is None:
            problems = self.problems
            return False, (problems[0] if problems else Msg(N_("The MapleJet settings haven't loaded yet.")))
        if self.too_new:
            return False, _too_new(self.too_new[0], FORMAT + 1).msg
        reached = current.reached_at
        age = self.clock.time() - reached if reached is not None else None
        if age is not None and -CLOCK_BACK_TOLERANCE_S <= age <= self.timings.local_copy_max_age_s:
            return True, None
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(reached)) if reached is not None else "?"
        if not self.store.available:
            return False, Msg(N_("Can't reach the server since {time}."), {"time": when})
        return False, Msg(N_("Stripes hasn't been able to read the templates from the server since {time}."),
                          {"time": when})

    # saving records

    def _require_admin(self):
        current = self.current
        if current is None or not self.identity.is_admin(current.company):
            raise NotAllowed(Msg(N_("Only Stripes Administrators can change this.")))

    def save(self, record, *, expected_revision):
        """Blocking (worker thread only): revision + 1, write, read back, changes.json, the admin log,
        reload. Raises NotAllowed, RevisionConflict, FormatTooNew, ShareUnavailable, ShareDenied,
        ShareError and ValueError. A changes.json write that fails is raised last, after the record
        and its log entry are written: the save happened, but other PCs don't know about it yet."""
        self._require_admin()
        unannounced = []        # changes.json writes that failed: other PCs don't know about the save
        saved = self._save_one(record, expected_revision, unannounced)
        if isinstance(saved, Calibration) and saved.head_distance_mm is not None:
            rel = record_rel_for(Machine, saved.machine_id)
            raw = self.store.read_bytes(rel)
            if raw is not None:
                machine = from_json(Machine, decode_json(raw, file_type="machine", rel=rel))
                if machine.head_distance_mm != saved.head_distance_mm:
                    self._save_one(dataclasses.replace(machine, head_distance_mm=saved.head_distance_mm),
                                   machine.revision, unannounced)
        self.reload()
        if unannounced:
            # The record is saved and logged, but other PCs keep their old data until their forced
            # full reload, so the administrator must hear about it
            raise unannounced[0]
        return saved

    def _save_one(self, record, expected_revision, unannounced):
        cls = type(record)
        if cls not in FILE_TYPES:
            raise ValueError(f"{cls.__name__} isn't a stored record")
        record_type = FILE_TYPES[cls]
        _check_record(record)
        rel = record_rel(record)
        store = self.store
        raw = store.read_bytes(rel)
        current, damaged = None, False
        if raw is not None:
            try:
                if len(raw) > RECORD_MAX_BYTES:
                    raise ValueError("too big")
                current = decode_json(raw, file_type=record_type, rel=rel)   # FormatTooNew goes up
                from_json(cls, current)
            except ValueError:
                current, damaged = None, True

        def conflict(d):
            user = d.get("updated_by") if d and isinstance(d.get("updated_by"), str) else "?"
            revision = d.get("revision") if d and _is_int(d.get("revision")) else 0
            return RevisionConflict(Msg(N_("{user} changed this while you were editing it. Reload it and make "
                                           "your change again."), {"user": user}), current=revision)

        if expected_revision is None:
            if raw is not None:
                raise conflict(current)
            revision = 1
        elif damaged:
            revision = expected_revision + 1    # an edit may repair a file nobody can read
        elif current is None or current.get("revision") != expected_revision:
            raise conflict(current)
        else:
            revision = current["revision"] + 1

        body = to_json(record)
        body["revision"] = revision
        body["updated_by"] = self.identity.user
        body["updated_at"] = self.clock.time()
        names = {f.name for f in dataclasses.fields(cls)}
        for key, value in (current or {}).items():
            if key not in ENVELOPE_KEYS and key not in names:
                body[key] = value               # a newer minor version's field survives
        data = {"file_type": record_type, **body}
        if len(encode_json(record_type, data)) > RECORD_MAX_BYTES:
            raise ValueError(f"{rel} would be too big")
        if cls is Film and raw is None:
            store.makedirs(f"templates/films/{record.id}")
        store.write_json(rel, data)

        # Read it back: another administrator saving in the same moment wins, and both are logged
        problem = None
        try:
            back = decode_json(store.read_bytes(rel) or b"", file_type=record_type, rel=rel)
            if (back.get("revision"), back.get("updated_by"), back.get("updated_at")) != \
                    (revision, body["updated_by"], body["updated_at"]):
                problem = conflict(back)
        except (ShareError, ValueError) as e:
            problem = e if isinstance(e, ShareError) else conflict(None)
        try:
            self._bump_changes(record_type, record.id, revision)
        except ShareError as e:
            with self._lock:
                self._full_asked += 1   # this PC's own reload mustn't skip the save either
            unannounced.append(e)
        self.append_log("hx-admin", {
            "type": "record_saved", "record_type": record_type, "record_id": record.id, "revision": revision,
            "previous_revision": current.get("revision") if current else None,
            "record": _masked(record_type, body), "previous": _masked(record_type, current)})
        if problem is not None:
            raise problem
        return from_json(cls, body)

    def _bump_changes(self, record_type, record_id, revision):
        rel = "config/hx/changes.json"
        raw = self.store.read_bytes(rel)
        d = _lenient_json(raw) if raw is not None else None
        counter = d.get("counter") if d and _is_int(d.get("counter")) else 0
        self.store.write_json(rel, {"file_type": "changes", "counter": counter + 1,
                                    "written_at": self.clock.time(), "written_by": self.identity.user,
                                    "last": {"file_type": record_type, "id": record_id, "revision": revision}})

    # film files and previews

    def save_file(self, film_id, kind, source_path):
        """Blocking: copies a film original onto the share and logs it; the caller adds the FileRef
        to the film and saves the film."""
        self._require_admin()
        if kind not in FILE_KINDS:
            raise ValueError(f"unknown file kind {kind!r}")
        _check_id(film_id, "film id")
        if os.path.getsize(source_path) > ATTACHMENT_MAX_BYTES:
            raise ShareError(Msg(N_("The file is too big (at most {mb} MB)."),
                                 {"mb": ATTACHMENT_MAX_BYTES // 1_000_000}))
        digest, size = hashlib.sha256(), 0
        with open(source_path, "rb") as f:
            for chunk in iter(lambda: f.read(CHUNK_BYTES), b""):
                digest.update(chunk)
                size += len(chunk)
        sha = digest.hexdigest()
        name = os.path.basename(source_path)
        path = f"films/{film_id}/files/{sha[:8]}_{safe_file_name(name)}"
        if self.store._size("templates/" + path) != size:
            self.store.makedirs(f"templates/films/{film_id}/files")
            self.store.copy_in("templates/" + path, source_path)
        self.append_log("hx-admin", {"type": "file_added", "film_id": film_id, "kind": kind, "name": name,
                                     "path": path, "sha256": sha, "size": size})
        return FileRef(kind=kind, name=name, path=path, sha256=sha, size=size, added_by=self.identity.user,
                       added_at=self.clock.time())

    def save_preview(self, film_id, sha256, png):
        self._require_admin()
        _check_id(film_id, "film id")
        if not isinstance(sha256, str) or not _SHA_RE.match(sha256):
            raise ValueError(f"bad sha256 {sha256!r}")
        if len(png) > FILM_PREVIEW_MAX_BYTES or not bytes(png).startswith(PNG_SIGNATURE):
            raise ValueError("a film preview is a PNG of at most 512,000 bytes")
        self.store.makedirs(f"templates/films/{film_id}/previews")
        self.store.write_bytes(f"templates/films/{film_id}/previews/{sha256}.png", png)

    def preview_bytes(self, film_id, sha256):
        """Blocking: the local cache, else the share's preview (then cached); None if neither has it."""
        try:
            with open(self.local.preview_path(sha256), "rb") as f:
                return f.read()
        except OSError:
            pass
        _check_id(film_id, "film id")
        try:
            png = self.store.read_bytes(f"templates/films/{film_id}/previews/{sha256}.png")
        except ShareError:
            return None
        if png is None:
            return None
        try:
            self.local.write_preview(sha256, png)
        except OSError:
            pass
        return png

    def share_path(self, rel):
        """The absolute share path of a templates-relative or share-relative path; None without a share."""
        if rel.split("/", 1)[0] not in ("config", "templates", "status", "logs"):
            rel = "templates/" + rel
        return self.store.full_path(rel)

    # logs and the queue

    def append_log(self, kind, entry):
        """Blocking: (file name, queued). Queued on this PC when the share can't be written, so no
        entry is lost."""
        at = self.clock.time()
        stored = make_log_record(kind, entry, self.identity, at)
        name = log_name(entry, at)
        raw = encode_log(stored)
        if self.store.available:
            try:
                return self.store.put_log_bytes(kind, name, raw), False
            except ShareError:
                pass
        try:
            self.local.queue_log(kind, name, stored)
        except OSError as e:
            raise ShareError(Msg(N_("Stripes couldn't save the log entry on this PC: {error}"),
                                 {"error": _error_text(e)})) from None
        return name, True

    def append_send_log(self, entry):
        return self.append_log("hx-sends", entry)

    def _flush(self):
        """Uploads queued entries this user may add (storage.md §13.6)."""
        current = self.current
        if not self.store.available or current is None:
            return
        allowed = {"hx-events"}
        if self.identity.is_sender(current.company):
            allowed.add("hx-sends")
        if self.identity.is_admin(current.company):
            allowed.add("hx-admin")
        try:
            queued = [q for q in self.local.queued_logs() if q[0] in allowed][:200]
        except OSError:
            return
        denied = set()
        for kind, name, stored in queued:
            if kind in denied:
                continue
            try:
                self.store.put_log_bytes(kind, name, encode_log(stored))
            except ShareUnavailable:
                return
            except ShareDenied:
                denied.add(kind)
                continue
            except (ShareError, ValueError):
                continue
            try:
                self.local.mark_uploaded(name)
            except OSError as e:
                print(f"Could not mark {name} as uploaded: {e}", file=sys.stderr)

    def pending_logs(self):
        """Queued send-log entries ("N log entries waiting")."""
        try:
            return len(self.local.pending_send_logs())
        except OSError:
            return 0

    def find_send(self, send_id):
        """Blocking: the main log entry of a Send, from the share, else from this PC's queue."""
        try:
            names = self.store.list_logs("hx-sends", since=self.clock.time() - 400 * 86_400, entry_type="send",
                                         ref=_ref_part(send_id))
            for name in names:
                entry = self.store.read_log("hx-sends", name)
                if entry is not None and entry.get("send_id") == send_id:
                    return entry
        except ShareError:
            pass
        try:
            queued = self.local.queued_logs()
        except OSError:
            return None
        for kind, name, stored in queued:
            if kind == "hx-sends" and stored.get("type") == "send" and stored.get("send_id") == send_id:
                return dict(stored, log_name=name)
        return None
