"""Talking to MapleJet Hx printers: Autopilot commands and the web read-back.

Every printer request goes through a PrinterClient. HTTP is written by hand on a raw
socket, one request per connection, so no proxy setting can apply and the bytes are
the ones that were checked on a real printer. A per-printer gate keeps this process
to one request at a time to each printer (the printer mixes up replies otherwise).

Reads are checked against the query catalogue (a code such as 7 "busy" comes back as
plain text and can look like a value) and asked once more if they fail. Writes are
checked against the whitelist and never repeated. The web Load reads back the
printer's own copy of a stored message; the printer keeps one reply buffer for every
client, so the client repeats until the reply carries its own request numbers.

The printer password is kept only as a token, scrambled with the release key. Every
method blocks: call them from worker threads, never from the Tk thread.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import socket
import struct
import threading
import time
import zlib
from dataclasses import dataclass, field
from datetime import date, datetime

from hx_core import SYSTEM_CLOCK, Msg
from hx_model import (ALLOWED_WRITE_TAGS, MESSAGE_NAME_MAX, TIMINGS, AuthFailed, BadReply, Command,
                      CommandRefused, ForbiddenCommand, MessageDesign, DesignItem, NoReply, PrinterBusy,
                      WebReadFailed, WrongPrinter)
from i18n import N_, add_spanish

ES = {
    "No reply from {printer}.": "Sin respuesta de {printer}.",
    "No reply from {printer} to {command}. It may or may not have been done.":
        "Sin respuesta de {printer} a {command}. Puede que se haya hecho o no.",
    "{printer} is busy.": "{printer} está ocupada.",
    "{printer} is busy with another request from this PC.":
        "{printer} está ocupada con otra solicitud de este PC.",
    "{printer} refused the password, or another printer is at its address.":
        "{printer} rechazó la contraseña, o hay otra impresora en su dirección.",
    "No password is saved for {printer}.": "No hay ninguna contraseña guardada para {printer}.",
    "A saved printer password was made by a different Stripes build. Enter it again.":
        "Una contraseña de impresora guardada se creó con otra compilación de Stripes. Escríbala de nuevo.",
    "A saved printer password is damaged. Enter it again.":
        "Una contraseña de impresora guardada está dañada. Escríbala de nuevo.",
    "{printer}: {problem}": "{printer}: {problem}",
    "{printer} gave an answer to {query} that doesn't make sense: {reply}":
        "{printer} dio una respuesta a {query} que no tiene sentido: {reply}",
    "{printer} gave two different answers to {query}.": "{printer} dio dos respuestas distintas a {query}.",
    "{printer} refused {command} (code {code}).": "{printer} rechazó {command} (código {code}).",
    "{printer} is not the expected printer: UID {got} answered instead of {expected}.":
        "{printer} no es la impresora esperada: respondió el UID {got} en lugar del {expected}.",
    "{printer} didn't send back the message in slot {slot} in time.":
        "{printer} no devolvió a tiempo el mensaje de la ranura {slot}.",
    "{printer} sent back a message Stripes doesn't recognise.":
        "{printer} devolvió un mensaje que Stripes no reconoce.",
    "{printer} sent back slot {got} instead of slot {slot}.":
        "{printer} devolvió la ranura {got} en lugar de la ranura {slot}.",
    "{printer} didn't send its font list.": "{printer} no envió su lista de fuentes.",
    "Stripes tried to send a command it never sends: {command}.":
        "Stripes intentó enviar un comando que nunca envía: {command}.",
}
add_spanish(ES)

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

# The only requests Stripes makes (protocol.md §7)
AUTOPILOT_PATH = "/"                              # POST, body starts "<Cookie>"
LOGIN_PATH = "/"                                  # POST, body starts "Type=LOG"
SME_PATH = "/simple_msg.html?sme"                 # POST: Load
SME_REPEAT_PATH = "/simple_msg.html?sme_repeat"   # POST: RepeatRequest
FONT_FILE = "/sfl.xml"                            # GET: the font list. OPEN
IDENTITY_FILE = "/idn.xml"                        # GET: the firmware version. OPEN
WEB_FILES = (FONT_FILE, IDENTITY_FILE)
EDITOR_PATH = "/simple_msg.html"                  # the 2.3 MB editor page: never requested

# Limits, in bytes
MAX_HEADER_BYTES = 16_384
MAX_AUTOPILOT_REPLY = 4_096
MAX_SME_REPLY = 262_144          # a Load reply is about 29.5 KB
MAX_SME_DEPTH = 16               # elements open at once, SM included; a real Load reply goes 4 deep (§11.1)
MAX_FILE_REPLY = 65_536
MAX_LOGIN_REPLY = 1_048_576      # read and discarded; may be cut short

PREVIEW_PREFIX = "data:image/bmp;base64,"
PREVIEW_BYTES = 20_542           # 62-byte header + 128 bytes x 160 rows
STRIPES_TN_MOD = 97
STRIPES_TN_REM = 42              # every Stripes TN: int(tn) % 97 == 42
FIRMWARE_TAGS = ("FIRMWARE", "FWVER", "FW", "VERSION", "VER")   # OPEN
DEV_KEY = b"stripes-hx-development-key-v1"   # public; used when hx_key is absent
SCRAMBLE_PREFIX = "hx1"

REPLY_CODES = {-1: "invalid query", 0: "done", 1: "invalid command", 2: "invalid syntax",
               3: "invalid parameter", 4: "not acceptable now", 5: "not enough data",
               6: "timeout", 7: "busy"}           # manual §3
BUSY_REPLIES = ("6", "7")

_ITEM = ("OBJID", "POS", "FONTIND", "SIZE", "MARG", "PREF", "POSF", "XRID", "YRID", "SXAS", "SYAS")
WRITE_CHILDREN = {
    "CTEXT": _ITEM + ("TXT",),
    "CTDAT": _ITEM + ("FRMT", "DAY", "MON", "YEAR"),
    "CPPARM": ("DELAY", "PRNSPD", "ENCSCL", "DLYMM", "BOLD", "WDTH", "QUANT", "QDELAY", "QDLYMM",
               "ENCST", "RESMOD", "ROTAT", "MIRR", "REVRS", "AUTREP"),
    "TD": ("DAY", "MNTH", "YEAR", "HOUR", "MIN", "DTRH", "DTRM"),
}
# Printer-wide setting commands: cartridge, device and sensor, encoder, serial port, auto-notify,
# online data, counter reset, printer name, beacon output, economy mode, lot slots (§7)
PRINTER_WIDE_WRITES = ("CRT", "DEV", "ENC", "RS232PRM", "AUTONOT", "ODPARAM", "SETPPARAM", "RSTPMC",
                       "HXNA", "EXBECMD", "ECOMOD") + tuple(f"LOT{i}" for i in range(1, 10))
# Message objects Stripes doesn't use: lots, counters, shifts, external data, barcodes
UNUSED_WRITES = ("CLOT", "CCONT", "CSHIF", "CEXTN", "CBARC")

_REPEAT_BODY = b"<SM>RepeatRequest</SM>"
_U32 = 4_294_967_295


# --------------------------------------------------------------------------
# Messages
# --------------------------------------------------------------------------

def _no_reply(printer):
    return NoReply(Msg(N_("No reply from {printer}."), {"printer": printer}))


def _busy(printer):
    return PrinterBusy(Msg(N_("{printer} is busy."), {"printer": printer}))


def _gate_busy(printer):
    return PrinterBusy(Msg(N_("{printer} is busy with another request from this PC."), {"printer": printer}))


def _refused_password(printer):
    return AuthFailed(Msg(N_("{printer} refused the password, or another printer is at its address."),
                          {"printer": printer}))


def _damaged():
    return AuthFailed(Msg(N_("A saved printer password is damaged. Enter it again.")))


def _bad(printer, tag, reply):
    reply = reply[:40]
    return BadReply(Msg(N_("{printer} gave an answer to {query} that doesn't make sense: {reply}"),
                        {"printer": printer, "query": tag, "reply": reply}), tag=tag, reply=reply)


def _two_answers(printer, tag, a, b):
    return BadReply(Msg(N_("{printer} gave two different answers to {query}."), {"printer": printer, "query": tag}),
                    tag=tag, reply=f"{a} / {b}"[:40])


def _unrecognised(printer=""):
    return WebReadFailed(Msg(N_("{printer} sent back a message Stripes doesn't recognise."), {"printer": printer}))


def _not_in_time(printer, slot):
    return WebReadFailed(Msg(N_("{printer} didn't send back the message in slot {slot} in time."),
                             {"printer": printer, "slot": slot}))


def _no_fonts(printer, reply=""):
    return BadReply(Msg(N_("{printer} didn't send its font list."), {"printer": printer}),
                    tag="FONTS", reply=reply[:40])


def _forbidden(tag):
    return ForbiddenCommand(Msg(N_("Stripes tried to send a command it never sends: {command}."),
                                {"command": tag}), tag=tag)


def _relabel(error, printer):
    """The same error with this printer's name in its message (the parser doesn't know it)."""
    values = dict(error.msg.values, printer=printer)
    copy = type(error).__new__(type(error))
    copy.__dict__.update(error.__dict__)
    copy.msg = Msg(error.msg.text, values)
    copy.args = (copy.msg,)
    return copy


# --------------------------------------------------------------------------
# Text and escaping
# --------------------------------------------------------------------------

def xml_escape(text):
    """& first, then < and >; nothing else (the same rule as hx_model.Command)."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _unescape(text):
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def decode_text(raw):
    """Strict UTF-8, else latin-1, so a single 0xFF byte survives. Never fails."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_INT = re.compile(r"-?\d{1,10}")


def clock_from(date_iso, time_iso):
    """read("DATE") and read("TIME") as the naive printer-local datetime. Judges nothing."""
    return datetime.fromisoformat(f"{date_iso}T{time_iso}")


# --------------------------------------------------------------------------
# Query catalogue and reply checks (§5)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ReplySpec:
    kind: str                    # "int" | "str" | "date" | "time"
    min: int | None = None
    max: int | None = None
    allowed: tuple | None = None # e.g. (0, 1) for PRIN
    none_ok: bool = False        # "None" (empty) is a valid answer


_BOOL = (0, 1)
QUERY_SPECS = {
    "HXUI": ReplySpec("int", 1, _U32),
    "HXNA": ReplySpec("str", max=50, none_ok=True),
    "PRIN": ReplySpec("int", allowed=_BOOL),
    "MSGNAM": ReplySpec("str", max=MESSAGE_NAME_MAX, none_ok=True),
    "MSCNT": ReplySpec("int", 0, _U32),
    "PMCNT": ReplySpec("int", 0, _U32),
    "DATE": ReplySpec("date"),
    "TIME": ReplySpec("time"),
    "INKT": ReplySpec("str", max=50, none_ok=True),
    "INKL": ReplySpec("int", allowed=(0, 1, 2, 3, 4)),
    "PHTP": ReplySpec("int", -30, 100),
    "ECOMOD": ReplySpec("int", allowed=_BOOL),
    "ENCST": ReplySpec("int", allowed=_BOOL),
    "ENWHD": ReplySpec("int", 20, 150),
    "ENPPR": ReplySpec("int", 1, 1_000_000),
    "ENTPM": ReplySpec("int", allowed=_BOOL),
    "PLDEL": ReplySpec("int", 10, 2550),
    "MNPL": ReplySpec("int", 50, 1500),
    "PRPL": ReplySpec("int", 50, 1000),
    "PLVO": ReplySpec("int", 7500, 13000),
    "PLWR": ReplySpec("int", 20, 45),
    "NZRO": ReplySpec("int", allowed=(1, 2, 3)),
    "DELAY": ReplySpec("int", 0, 120_000),       # ms, though DELAY is written in µs (§5.3)
    "PRNSPD": ReplySpec("int", 0, 2500),
    "RESMOD": ReplySpec("int", allowed=(0, 1, 2, 3)),
    "BOLD": ReplySpec("int", 1, 10),
    "WDTH": ReplySpec("int", 1, 10),
    "QUANT": ReplySpec("int", 1, 100),
    "ROTAT": ReplySpec("int", allowed=_BOOL),
    "MIRR": ReplySpec("int", allowed=_BOOL),
    "REVRS": ReplySpec("int", allowed=_BOOL),
    "AUTREP": ReplySpec("int", allowed=_BOOL),
}


def check_query(tag):
    """ForbiddenCommand for any tag outside the catalogue (the multi-value queries among them)."""
    if tag not in QUERY_SPECS:
        raise _forbidden(tag)


def check_reply(tag, text, *, printer=""):
    """The catalogue check of one stripped reply: the value, else PrinterBusy or BadReply."""
    if text in BUSY_REPLIES:
        raise _busy(printer)
    spec = QUERY_SPECS.get(tag)
    if spec is None:
        raise _forbidden(tag)
    if text == "-1":
        raise _bad(printer, tag, text)
    if text == "None":
        if spec.none_ok:
            return None
        raise _bad(printer, tag, text)
    value = _checked(spec, text)
    if value is None:
        raise _bad(printer, tag, text)
    return value


def _checked(spec, text):
    """The value of text by spec, or None when it fails."""
    if spec.kind == "int":
        if not _INT.fullmatch(text):
            return None
        n = int(text)
        if spec.allowed is not None:
            return n if n in spec.allowed else None
        if (spec.min is not None and n < spec.min) or (spec.max is not None and n > spec.max):
            return None
        return n
    if spec.kind == "str":
        if not 1 <= len(text) <= (spec.max or 0) or _CONTROL.search(text):
            return None
        if re.fullmatch(r"-?\d+", text) and -1 <= int(text) <= 7:
            return None         # a stray code must never pass as a name or an ink
        return text
    if spec.kind == "date":
        m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", text)      # day/month/year
        if not m:
            return None
        day, month, year = (int(g) for g in m.groups())
        if not 2000 <= year <= 2050:
            return None
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            return None
    if spec.kind == "time":
        m = re.fullmatch(r"(\d{1,2}):(\d{2}):(\d{2})", text)
        if not m:
            return None
        hour, minute, second = (int(g) for g in m.groups())
        if hour > 23 or minute > 59 or second > 59:
            return None
        return f"{hour:02d}:{minute:02d}:{second:02d}"
    return None


# --------------------------------------------------------------------------
# The write whitelist (§6)
# --------------------------------------------------------------------------

def _int_in(lo, hi):
    def check(v):
        return bool(_INT.fullmatch(v)) and lo <= int(v) <= hi
    return check


def _text_up_to(lo, hi):
    def check(v):
        # Lengths count characters before escaping; a raw ">" or a lone "&" wasn't escaped
        if ">" in v or re.search(r"&(?!amp;|lt;|gt;)", v):
            return False
        plain = _unescape(v)
        return lo <= len(plain) <= hi and not _CONTROL.search(plain)
    return check


def _point(v):
    return bool(re.fullmatch(r"-?\d{1,5},-?\d{1,5}", v))


_SLOT = lambda v: v in ("9", "10")
_US = _int_in(0, 120_000_000)
_FLAG = _int_in(0, 1)
_ITEM_CHECKS = {
    "OBJID": _int_in(10000, 20000), "POS": _point, "FONTIND": _int_in(1, 6), "SIZE": _int_in(1, 10),
    "MARG": _point, "PREF": _text_up_to(0, 100), "POSF": _text_up_to(0, 100),
    "XRID": _int_in(10000, 20000), "YRID": _int_in(10000, 20000), "SXAS": _FLAG, "SYAS": _FLAG,
}
_CHILD_CHECKS = {
    "CTEXT": dict(_ITEM_CHECKS, TXT=_text_up_to(0, 250)),
    "CTDAT": dict(_ITEM_CHECKS, FRMT=_text_up_to(1, 100), DAY=_int_in(0, 999), MON=_int_in(0, 999),
                  YEAR=_int_in(0, 99)),
    "CPPARM": {"DELAY": _US, "PRNSPD": _int_in(1, 2500), "ENCSCL": _int_in(0, 100), "DLYMM": _US,
               "BOLD": _int_in(1, 10), "WDTH": _int_in(1, 10), "QUANT": _int_in(1, 100), "QDELAY": _US,
               "QDLYMM": _US, "ENCST": _FLAG, "RESMOD": _int_in(0, 3), "ROTAT": _FLAG, "MIRR": _FLAG,
               "REVRS": _FLAG, "AUTREP": _FLAG},
    # HOUR and DTRH 0..24, DTRM 0..59: wider than the manual's, pending the test unit (OPEN)
    "TD": {"DAY": _int_in(1, 31), "MNTH": _int_in(1, 12), "YEAR": _int_in(2000, 2050), "HOUR": _int_in(0, 24),
           "MIN": _int_in(0, 59), "DTRH": _int_in(0, 24), "DTRM": _int_in(0, 59)},
}
_SET_CHECKS = {"CRTMSG": _text_up_to(0, MESSAGE_NAME_MAX), "SAVEMSG": _SLOT, "PRINTMSG": _SLOT,
               "PRINT": lambda v: v in ("0", "1")}
_LEAF = re.compile(r"<([A-Z][A-Z0-9]*)>([^<]*)</\1>")


def check_write(cmd):
    """ForbiddenCommand unless cmd is exactly one whitelisted write with allowed children and values."""
    tag = getattr(cmd, "tag", "")
    if not isinstance(cmd, Command) or not cmd.write or tag not in ALLOWED_WRITE_TAGS:
        raise _forbidden(tag)
    m = re.fullmatch(rf"<{tag}>(.*)</{tag}>", cmd.xml, re.DOTALL)
    if not m:
        raise _forbidden(tag)
    inner = m.group(1)
    if tag in _SET_CHECKS:
        if "<" in inner or not _SET_CHECKS[tag](inner):
            raise _forbidden(tag)
        return
    checks = _CHILD_CHECKS[tag]
    seen = set()
    pos = 0
    for leaf in _LEAF.finditer(inner):
        child, value = leaf.groups()
        if leaf.start() != pos or child not in checks or child in seen or not checks[child](value):
            raise _forbidden(tag)
        seen.add(child)
        pos = leaf.end()
    if pos != len(inner):
        raise _forbidden(tag)       # something that isn't a leaf, such as a second command


# --------------------------------------------------------------------------
# Request bytes (§2.3)
# --------------------------------------------------------------------------

def _host_header(host, port):
    return host if port == 80 else f"{host}:{port}"


def _cookie_header(cookies):
    # Access_token_<uid> first, then Auth_token_<uid>, as the browser sends them
    def rank(name):
        return 0 if name.startswith("Access_token_") else 1 if name.startswith("Auth_token_") else 2
    return "; ".join(f"{name}={cookies[name]}" for name in sorted(cookies, key=rank))


def autopilot_request(uid, token, xml):
    """Exactly as verified on the printer: no Host header, keep-alive (the printer closes anyway)."""
    body = f"<Cookie><Auth_token_{uid}>{token}</Auth_token_{uid}></Cookie><APCMD>{xml}</APCMD>".encode("utf-8")
    return b"POST / HTTP/1.1\r\nContent-Length: %d\r\nConnection: keep-alive\r\n\r\n" % len(body) + body


def login_request(host, port, md5_hex):
    body = f"Type=LOG&pass={md5_hex}&remember=on".encode("ascii")
    return (f"POST {LOGIN_PATH} HTTP/1.1\r\nHost: {_host_header(host, port)}\r\nConnection: close\r\n"
            f"Accept-Encoding: gzip\r\nContent-Type: application/x-www-form-urlencoded\r\n"
            f"Content-Length: {len(body)}\r\n\r\n").encode("ascii") + body


def load_body(tn, xid, slot):
    return f"<SM><TYPE>Load</TYPE><TN>{tn}</TN><XID>{xid}</XID><MSNO>{slot}</MSNO></SM>".encode("ascii")


def sme_request(host, port, cookies, *, repeat, body):
    path = SME_REPEAT_PATH if repeat else SME_PATH
    return (f"POST {path} HTTP/1.1\r\nHost: {_host_header(host, port)}\r\nConnection: close\r\n"
            f"Accept-Encoding: gzip\r\nCookie: {_cookie_header(cookies)}\r\n"
            f"Content-Type: text/plain;charset=UTF-8\r\nContent-Length: {len(body)}\r\n\r\n"
            ).encode("utf-8") + body


def file_request(host, port, cookies, path):
    """GET one of WEB_FILES; any other path (the editor page above all) is a programming error."""
    if path not in WEB_FILES:
        raise ValueError(f"not a printer file Stripes reads: {path}")
    return (f"GET {path} HTTP/1.1\r\nHost: {_host_header(host, port)}\r\nConnection: close\r\n"
            f"Accept-Encoding: gzip\r\nCookie: {_cookie_header(cookies)}\r\n\r\n").encode("utf-8")


# --------------------------------------------------------------------------
# HTTP on a raw socket (§2.2)
# --------------------------------------------------------------------------

@dataclass(kw_only=True)
class HttpReply:
    status: int
    headers: list[tuple[str, str]] = field(default_factory=list)   # in the order received
    body: bytes = b""                     # gzip already undone unless gunzip=False
    complete: bool = True                 # False only when allow_cut_short cut it short

    def header(self, name):
        """The first header of that name (any case), or None."""
        name = name.lower()
        for key, value in self.headers:
            if key.lower() == name:
                return value
        return None

    def cookies(self):
        """Every Set-Cookie's "name=value", up to the first ";"."""
        found = {}
        for key, value in self.headers:
            if key.lower() == "set-cookie":
                pair = value.split(";", 1)[0]
                if "=" in pair:
                    name, _, val = pair.partition("=")
                    found[name.strip()] = val.strip()
        return found


_STATUS_LINE = re.compile(r"HTTP/1\.[01] (\d{3})( .*)?")
_HEADER_LINE = re.compile(r"([!#$%&'*+.^_`|~0-9A-Za-z-]+):[ \t]*(.*?)[ \t]*")


def _framing(printer):
    return _bad(printer, "", "HTTP framing")


def _gunzip(data, limit, printer, partial=False):
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        out = d.decompress(data, limit)
    except zlib.error:
        raise _bad(printer, "", "bad gzip") from None
    if d.unconsumed_tail or not (d.eof or partial):
        raise _bad(printer, "", "bad gzip")
    return out


def _recv(sock, deadline, printer):
    left = deadline - time.monotonic()
    if left <= 0:
        raise _no_reply(printer)
    try:
        sock.settimeout(left)
        return sock.recv(65536)
    except OSError:              # socket.timeout included
        raise _no_reply(printer) from None


def http_exchange(host, port, request, *, timeout_s, max_body, label="", allow_cut_short=False, gunzip=True):
    """One connection, one request, one reply, all within timeout_s. Raises NoReply or BadReply."""
    printer = label or host
    deadline = time.monotonic() + timeout_s
    try:
        sock = socket.create_connection((host, port), timeout=timeout_s)
    except OSError:
        raise _no_reply(printer) from None
    with sock:
        try:
            sock.sendall(request)
        except OSError:
            raise _no_reply(printer) from None
        data = bytearray()
        while True:
            end = data.find(b"\r\n\r\n")
            if end >= 0:
                break
            if len(data) > MAX_HEADER_BYTES:
                raise _framing(printer)
            chunk = _recv(sock, deadline, printer)
            if not chunk:
                raise _no_reply(printer)      # connection closed without reply (the idle drop)
            data += chunk
        if end > MAX_HEADER_BYTES:
            raise _framing(printer)
        lines = bytes(data[:end]).decode("latin-1").split("\r\n")
        status = _STATUS_LINE.fullmatch(lines[0])
        if not status:
            raise _framing(printer)
        headers = []
        for line in lines[1:]:
            m = _HEADER_LINE.fullmatch(line)
            if not m:
                raise _framing(printer)
            headers.append((m.group(1), m.group(2)))
        reply = HttpReply(status=int(status.group(1)), headers=headers)
        if "chunked" in (reply.header("Transfer-Encoding") or "").lower():
            raise _framing(printer)
        body = bytearray(data[end + 4:])
        complete = True
        length = reply.header("Content-Length")
        if length is not None:
            if not re.fullmatch(r"\d{1,12}", length):
                raise _framing(printer)
            length = int(length)
            if length > max_body and not allow_cut_short:
                raise _bad(printer, "", "reply too large")
            want = min(length, max_body)
            while len(body) < want:
                try:
                    chunk = _recv(sock, deadline, printer)
                except NoReply:
                    if not allow_cut_short:
                        raise
                    chunk = b""
                if not chunk:
                    if not allow_cut_short:
                        raise _no_reply(printer)
                    complete = False
                    break
                body += chunk
            del body[want:]
            if length > want:
                complete = False
        else:
            while True:
                if len(body) > max_body:
                    if not allow_cut_short:
                        raise _bad(printer, "", "reply too large")
                    del body[max_body:]
                    complete = False
                    break
                try:
                    chunk = _recv(sock, deadline, printer)
                except NoReply:
                    if not allow_cut_short:
                        raise
                    complete = False
                    break
                if not chunk:
                    break
                body += chunk
    body = bytes(body)
    if gunzip and (reply.header("Content-Encoding") or "").strip().lower() == "gzip":
        body = _gunzip(body, 4 * max_body, printer, partial=not complete)
    reply.body = body
    reply.complete = complete
    return reply


# --------------------------------------------------------------------------
# Token and scrambling (§3)
# --------------------------------------------------------------------------

def make_token(password):
    """base64 of the ASCII md5 hex of the password: what Autopilot wants in every request."""
    return base64.b64encode(hashlib.md5(password.encode("utf-8")).hexdigest().encode("ascii")).decode("ascii")


def password_md5(token):
    """The md5 hex back from a token, for the web login. AuthFailed when the token is damaged."""
    try:
        text = base64.b64decode(token.encode("ascii"), validate=True).decode("ascii")
    except (ValueError, UnicodeError, AttributeError):
        raise _damaged() from None
    if not re.fullmatch(r"[0-9a-f]{32}", text):
        raise _damaged()
    return text


def key_id(key):
    """8 hex characters naming a scrambling key."""
    return hashlib.sha256(b"stripes-hx-key-id" + key).hexdigest()[:8]


def is_development_key(key):
    return key == DEV_KEY


def _keystream(enc, nonce, n):
    out = bytearray()
    i = 0
    while len(out) < n:
        out += hashlib.sha256(enc + nonce + i.to_bytes(4, "big")).digest()
        i += 1
    return bytes(out[:n])


def scramble(token, key):
    """The stored form of a token (Printer.token): hx1:<key id>:<nonce + ciphertext + tag>."""
    try:
        password_md5(token)
    except AuthFailed:
        raise ValueError("scramble needs a token made by make_token") from None
    data = token.encode("ascii")
    enc = hashlib.sha256(b"stripes-hx-enc" + key).digest()
    mac = hashlib.sha256(b"stripes-hx-mac" + key).digest()
    nonce = secrets.token_bytes(16)
    ct = bytes(d ^ k for d, k in zip(data, _keystream(enc, nonce, len(data))))
    tag = hmac.new(mac, nonce + ct, hashlib.sha256).digest()[:16]
    payload = base64.urlsafe_b64encode(nonce + ct + tag).decode("ascii").rstrip("=")
    return f"{SCRAMBLE_PREFIX}:{key_id(key)}:{payload}"


def unscramble(stored, key):
    """The token back. AuthFailed for a damaged value or one made with another key."""
    parts = stored.split(":") if isinstance(stored, str) else []
    if len(parts) != 3 or parts[0] != SCRAMBLE_PREFIX:
        raise _damaged()
    if parts[1] != key_id(key):
        raise AuthFailed(Msg(N_("A saved printer password was made by a different Stripes build. Enter it again.")))
    payload = parts[2]
    try:
        raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
    except (ValueError, UnicodeError):
        raise _damaged() from None
    # Only the canonical spelling counts, so no changed character can slip through
    if len(raw) < 16 + 1 + 16 or base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=") != payload:
        raise _damaged()
    nonce, ct, tag = raw[:16], raw[16:-16], raw[-16:]
    mac = hashlib.sha256(b"stripes-hx-mac" + key).digest()
    if not hmac.compare_digest(hmac.new(mac, nonce + ct, hashlib.sha256).digest()[:16], tag):
        raise _damaged()
    enc = hashlib.sha256(b"stripes-hx-enc" + key).digest()
    data = bytes(c ^ k for c, k in zip(ct, _keystream(enc, nonce, len(ct))))
    try:
        token = data.decode("ascii")
    except UnicodeDecodeError:
        raise _damaged() from None
    password_md5(token)
    return token


def release_key():
    """hx_key.KEY when the release build made it, else the public development key."""
    try:
        import hx_key
    except ImportError:
        return DEV_KEY
    key = getattr(hx_key, "KEY", None)
    if not isinstance(key, bytes) or len(key) < 16:
        raise RuntimeError("hx_key.KEY must be at least 16 bytes")
    return key


# --------------------------------------------------------------------------
# Web helpers: TN, the reply envelope, the preview, the small files (§10)
# --------------------------------------------------------------------------

def new_tn():
    """Six digits with int % 97 == 42, so other Stripes PCs' leftovers can be told from a browser's."""
    return str(STRIPES_TN_MOD * (1031 + secrets.randbelow(9278)) + STRIPES_TN_REM)


def is_stripes_tn(tn):
    return bool(re.fullmatch(r"[1-9]\d{5}", tn or "")) and int(tn) % STRIPES_TN_MOD == STRIPES_TN_REM


def _skip_start(body):
    if body.startswith(b"\xef\xbb\xbf"):
        body = body[3:]
    return body.lstrip(b" \t\r\n")


def sme_envelope(body):
    """(TYPE, TN, XID, MSNO) texts of an SME reply, from before any FCDOC or PRV; None without <SM>.
    A missing TN, XID or MSNO gives ""; a missing TYPE gives None."""
    body = _skip_start(body)
    if not body.startswith(b"<SM>") or b"</SM>" not in body:
        return None
    cut = len(body)
    for marker in (b"<FCDOC>", b"<PRV>"):
        i = body.find(marker)
        if i >= 0:
            cut = min(cut, i)
    head = body[:cut]

    def leaf(name):
        m = re.search(rb"<%s>([^<]*)</%s>" % (name, name), head)
        return decode_text(m.group(1)) if m else None

    type_ = leaf(b"TYPE")
    return type_, leaf(b"TN") or "", leaf(b"XID") or "", leaf(b"MSNO") or ""


def classify_reply(body, *, tn, xid):
    """"match" | "stale" | "foreign" | "odd" | "busy" (§10.5)."""
    if body.strip() in (b"6", b"7"):
        return "busy"
    env = sme_envelope(body)
    if env is None or env[0] is None:
        return "odd"
    type_, reply_tn, reply_xid, _ = env
    if reply_tn == "":
        return "stale"          # a result Autopilot left, such as SAVEMSG's
    if reply_tn == tn:
        if not re.fullmatch(r"\d{1,10}", reply_xid):
            return "odd"
        n = int(reply_xid)
        if n == xid:
            return "match" if type_ == "Load" else "odd"
        return "stale" if n < xid else "odd"
    if is_stripes_tn(reply_tn):
        return "stale"          # another Stripes PC's leftover
    return "foreign"            # another web client used the printer


def decode_preview(uri):
    """The BMP bytes of a preview data URI; WebReadFailed unless the 1-bit 1024 x 160 BMP seen."""
    if not isinstance(uri, str) or not uri.startswith(PREVIEW_PREFIX):
        raise _unrecognised()
    text = re.sub(r"[ \t\r\n\f\v]", "", uri[len(PREVIEW_PREFIX):])
    try:
        data = base64.b64decode(text, validate=True)
    except (ValueError, UnicodeError):
        raise _unrecognised() from None
    if len(data) < 62 or data[:2] != b"BM":
        raise _unrecognised()
    size, offset, header = struct.unpack_from("<I4xII", data, 2)
    width, height, planes, bits, compression = struct.unpack_from("<iiHHI", data, 18)
    if (size != len(data) or len(data) != PREVIEW_BYTES or offset != 62 or header != 40 or width != 1024
            or height not in (160, -160) or planes != 1 or bits != 1 or compression != 0):
        raise _unrecognised()
    return data


def parse_fonts(body):
    """FON1, FON2, ... in order, up to the first missing or empty one. BadReply without FON1."""
    text = decode_text(body)
    found = {}
    for m in re.finditer(r"<FON([1-9])>([^<]*)</FON\1>", text):
        found.setdefault(int(m.group(1)), m.group(2).strip())
    fonts = []
    for i in range(1, 10):
        if not found.get(i):
            break
        fonts.append(found[i])
    if not fonts:
        raise _no_fonts("", text.strip())
    return fonts


def parse_firmware(body):
    """The stripped text (at most 40 characters) of the first FIRMWARE_TAGS leaf, else None."""
    text = decode_text(body)
    for tag in FIRMWARE_TAGS:
        m = re.search(rf"<{tag}>([^<]*)</{tag}>", text)
        if m and m.group(1).strip():
            return m.group(1).strip()[:40]
    return None


def worst_case_s(call, timings=TIMINGS):
    """The longest one PrinterClient call can take (§8.3); lock holders pass it to PrinterLock.check."""
    h, r = timings.http_timeout_s, timings.read_retry_s
    if call in ("read", "query", "probe", "login"):
        return 2 * (h + r)
    if call == "command":
        return 2 * (h + r) + h + r
    if call in ("read_twice", "check_uid"):
        return 4 * (h + r) + timings.second_reading_s
    if call == "load_slot":
        return 2 * timings.web_repeat_window_s + 2 * h
    if call in ("fonts", "firmware"):
        return 6 * (h + r)
    raise ValueError(f"no worst case for {call!r}")


# --------------------------------------------------------------------------
# SME reply parser (§11)
# --------------------------------------------------------------------------

@dataclass(kw_only=True)
class SmeReply:
    type: str                    # "Load", "Save", "Thumbnail", ...
    tn: str
    xid: str
    msno: int | None
    design: MessageDesign | None


class _Element:
    __slots__ = ("name", "start", "children", "value", "dirty")

    def __init__(self, name, start):
        self.name = name
        self.start = start       # just after the opening tag
        self.children = []
        self.value = None        # leaf value (bytes), set when it closes without children
        self.dirty = False       # holds text other than whitespace

    @property
    def leaf(self):
        return not self.children


_TAG = re.compile(rb"<(/?)([A-Za-z][A-Za-z0-9_]*)\s*(/?)>")
_WHITESPACE = b" \t\r\n"


def _parse_tree(body):
    """The SM element of an SME reply; WebReadFailed for anything the format doesn't allow."""
    data = bytes(body)
    pos = 3 if data.startswith(b"\xef\xbb\xbf") else 0
    while pos < len(data) and data[pos] in _WHITESPACE:
        pos += 1
    if data.startswith(b"<?xml", pos):
        end = data.find(b"?>", pos)
        if end < 0:
            raise _unrecognised()
        pos = end + 2
    root = None
    stack = []
    n = len(data)
    while pos < n:
        lt = data.find(b"<", pos)
        text = data[pos:] if lt < 0 else data[pos:lt]
        if text.strip(_WHITESPACE):
            if not stack:
                raise _unrecognised()     # text outside SM
            stack[-1].dirty = True
        if lt < 0:
            break
        m = _TAG.match(data, lt)
        if not m:
            raise _unrecognised()
        closing, name, empty = m.groups()
        pos = m.end()
        if closing:
            if empty or not stack or stack[-1].name != name:
                raise _unrecognised()
            el = stack.pop()
            if el.children:
                if el.dirty:
                    raise _unrecognised()  # text between the tags of a container
            else:
                el.value = data[el.start:lt]
            continue
        if root is not None and not stack:
            raise _unrecognised()          # something after </SM>
        el = _Element(name, pos)
        if empty:
            el.value = b""
        if stack:
            if stack[-1].dirty:
                raise _unrecognised()
            stack[-1].children.append(el)
        else:
            root = el
        if not empty:
            stack.append(el)
            if len(stack) > MAX_SME_DEPTH:
                raise _unrecognised()      # nesting no real reply has (and _leaves recurses per level)
    if stack or root is None or root.name != b"SM":
        raise _unrecognised()
    return root


def _leaves(el, prefix=""):
    """Every leaf under a container as (tag, text) in order; nested containers as "NESTED.LEAF"."""
    pairs = []
    for child in el.children:
        name = prefix + child.name.decode("ascii")
        if child.leaf:
            pairs.append((name, decode_text(child.value)))
        else:
            pairs.extend(_leaves(child, name + "."))
    return pairs


def _int_or_fail(text):
    if not _INT.fullmatch(text):
        raise _unrecognised()
    return int(text)


def parse_sme_reply(body):
    """Parse an SME reply, byte by byte as the printer sent it: values are kept unchanged (spaces
    included, no XML entity decoded), each strict UTF-8 else latin-1. Raises WebReadFailed."""
    sm = _parse_tree(body)
    fields = {}
    containers = {}
    for child in sm.children:
        name = child.name.decode("ascii")
        if child.leaf:
            fields.setdefault(name, decode_text(child.value))
        elif name in ("FCDOC", "PRV") and name not in containers:
            containers[name] = child
        else:
            raise _unrecognised()
    if "TYPE" not in fields:
        raise _unrecognised()
    type_ = fields["TYPE"]
    tn = fields.get("TN", "")
    xid = fields.get("XID", "")
    msno = fields.get("MSNO", "")
    msno = _int_or_fail(msno) if msno != "" else None
    design = None
    if type_ == "Load":
        design = _design(containers, msno, tn, xid)
    return SmeReply(type=type_, tn=tn, xid=xid, msno=msno, design=design)


def _design(containers, msno, tn, xid):
    fcdoc = containers.get("FCDOC")
    prv = containers.get("PRV")
    if prv is None:
        raise _unrecognised()
    prv_fields = {}
    for child in prv.children:
        if child.leaf:
            prv_fields.setdefault(child.name.decode("ascii"), decode_text(child.value))
    if "BPIX" not in prv_fields or "BMP" not in prv_fields or not re.fullmatch(r"\d{1,10}", prv_fields["BPIX"]):
        raise _unrecognised()
    design = MessageDesign(slot=msno if msno is not None else 0, name="", empty=fcdoc is None,
                           black_pixels=int(prv_fields["BPIX"]),
                           preview_bmp=decode_preview(prv_fields["BMP"]),
                           tn=tn, xid=int(xid) if re.fullmatch(r"\d{1,10}", xid) else 0)
    fc_name = None
    if fcdoc is not None:
        for child in fcdoc.children:
            name = child.name.decode("ascii")
            if child.leaf:
                value = decode_text(child.value)
                if name == "MSGNAM" and fc_name is None:
                    fc_name = value
                elif name == "W":
                    design.width = _int_or_fail(value)
                elif name == "H":
                    design.height = _int_or_fail(value)     # the last H
            elif name == "CPPARM":
                for key, value in _leaves(child):
                    design.settings[key] = value            # a repeated child keeps its last value
            else:
                design.items.append(DesignItem(tag=name, pairs=_leaves(child)))
    prv_name = prv_fields.get("MSGNAM")
    if fc_name is not None and prv_name is not None and fc_name != prv_name:
        raise _unrecognised()
    design.name = fc_name if fc_name is not None else (prv_name if prv_name is not None else "")
    return design


# --------------------------------------------------------------------------
# The per-printer gate (§9)
# --------------------------------------------------------------------------

class GateRegistry:
    """One threading.Lock per printer, so a process never has two requests in flight to it."""

    def __init__(self):
        self._lock = threading.Lock()
        self._gates = {}

    def gate(self, printer_key):
        """printer_key = f"{host}:{port}"."""
        with self._lock:
            gate = self._gates.get(printer_key)
            if gate is None:
                gate = self._gates[printer_key] = threading.Lock()
            return gate


PROCESS_GATES = GateRegistry()   # the app uses this one; a simulated PC in a test gets its own


class _Late(Exception):
    """Internal: the next exchange or sleep of a web call can't end by its deadline."""


# --------------------------------------------------------------------------
# PrinterClient
# --------------------------------------------------------------------------

class PrinterClient:
    """Talks to one printer. Thread-safe: every request, and the whole of load_slot, holds the
    printer's gate from the registry, so callers on different threads take turns."""

    def __init__(self, host, uid, token, *, port=80, clock=SYSTEM_CLOCK, gates=PROCESS_GATES, timings=TIMINGS):
        # token is the plain token (make_token); ClientPool unscrambles Printer.token before this
        self.host = host
        self.port = port
        self.uid = uid
        self.label = host            # the name used in messages; ClientPool sets the record's name
        self.clock = clock
        self.timings = timings
        self.tn = new_tn()
        self.xid = 0
        self.nonsense = 0            # replies thrown away because they make no sense (§13)
        self.busy_replies = 0        # "6" and "7" replies, kept apart from nonsense
        self.relogins = 0            # web logins after the first one
        self.last_request_at = None  # clock.monotonic() when the last exchange ended
        self._token = token
        self._cookies = {}           # the two login cookies, in memory only
        self._logged_in = False
        self._gate = gates.gate(f"{host}:{port}")
        self._counts = threading.Lock()   # the counters can also change between two gated reads

    # ---- counters

    def _add_nonsense(self):
        with self._counts:
            self.nonsense += 1

    def _add_busy(self):
        with self._counts:
            self.busy_replies += 1

    # ---- the gate

    def _take(self, call, deadline, late=None):
        """Take the gate, bounded by gate_wait_s and, with a deadline, by the latest time the call may
        start. late() makes the error for a deadline that can't be met (default: PrinterBusy)."""
        t = self.timings
        if deadline is None:
            if not self._gate.acquire(timeout=t.gate_wait_s):
                raise _gate_busy(self.label)
            return
        late = late or (lambda: _gate_busy(self.label))
        latest = deadline - (t.http_timeout_s if call in ("load_slot", "fonts", "firmware") else worst_case_s(call, t))
        wait = min(t.gate_wait_s, max(0.0, latest - self.clock.monotonic()))
        if not self._gate.acquire(timeout=wait):
            raise late()
        if self.clock.monotonic() > latest:
            self._gate.release()
            raise late()

    def _check(self, deadline, *, sleep=False):
        """Web calls: raise _Late unless the next exchange (and the sleep before it) can end by deadline."""
        if deadline is None:
            return
        need = self.timings.http_timeout_s + (self.timings.read_retry_s if sleep else 0.0)
        if self.clock.monotonic() + need > deadline:
            raise _Late()

    def _hold_after_no_reply(self, deadline=None):
        # The printer may still be working on the request that timed out, so the gate stays held
        # read_retry_s; with a deadline, only as long as it allows, so the call still ends by it
        r = self.timings.read_retry_s
        if deadline is not None:
            r = min(r, max(0.0, deadline - self.clock.monotonic()))
        if r > 0:
            self.clock.sleep(r)

    def _pause(self, deadline, *, no_reply=False):
        """Web calls: the read_retry_s gap before the next exchange. _Late when that exchange can't end
        by the deadline; after a NoReply the gate is still held as long as the deadline allows first."""
        try:
            self._check(deadline, sleep=True)
        except _Late:
            if no_reply:
                self._hold_after_no_reply(deadline)
            raise
        self.clock.sleep(self.timings.read_retry_s)

    # ---- one exchange (gate held)

    def _exchange(self, request, max_body):
        """(status, body) of one exchange. A 401's body is never read in full; a 200 cut short is
        "reply too large" (BadReply) or, when it simply stopped, NoReply."""
        try:
            reply = http_exchange(self.host, self.port, request, timeout_s=self.timings.http_timeout_s,
                                  max_body=max_body, label=self.label, allow_cut_short=True, gunzip=False)
        finally:
            self.last_request_at = self.clock.monotonic()
        if reply.status == 401:
            return 401, b""
        if not reply.complete:
            if len(reply.body) < max_body:
                raise _no_reply(self.label)
            raise _bad(self.label, "", "reply too large")
        body = reply.body
        if (reply.header("Content-Encoding") or "").strip().lower() == "gzip":
            body = _gunzip(body, 4 * max_body, self.label)
        return reply.status, body

    def _ask(self, xml, tag):
        """One Autopilot exchange: the stripped reply text. AuthFailed on 401; BadReply (nonsense) for
        another status, an empty reply or a broken one."""
        try:
            status, body = self._exchange(autopilot_request(self.uid, self._token, xml), MAX_AUTOPILOT_REPLY)
        except BadReply as e:
            self._add_nonsense()
            raise _bad(self.label, tag, e.reply) from None
        if status == 401:
            raise _refused_password(self.label)
        if status != 200:
            self._add_nonsense()
            raise _bad(self.label, tag, f"HTTP {status}")
        text = decode_text(body).strip()
        if not text:
            self._add_nonsense()
            raise _bad(self.label, tag, "")
        return text

    def _read_locked(self, tag, check=True, deadline=None):
        """At most two requests in all; a no-reply, a busy code or a failed check is asked once more
        after read_retry_s, the gate still held. Then the last error is raised. With a deadline the
        second request is sent only if it can still end by it (checked before and after the gap); if
        not, the first error is raised (§9 item 2)."""
        last = None
        for attempt in (1, 2):
            no_reply = False
            try:
                text = self._ask(f"<{tag}/>", tag)
                if text in BUSY_REPLIES:
                    self._add_busy()
                    raise _busy(self.label)
                if not check:
                    return text
                try:
                    return check_reply(tag, text, printer=self.label)
                except BadReply:
                    self._add_nonsense()
                    raise
            except NoReply as e:
                last, no_reply = e, True
            except (PrinterBusy, BadReply) as e:
                last = e
            if attempt == 1:
                try:
                    self._pause(deadline, no_reply=no_reply)
                    self._check(deadline)
                except _Late:
                    raise last from None
            elif no_reply:
                self._hold_after_no_reply(deadline)
        raise last

    # ---- Autopilot

    def query(self, tag, *, deadline=None):
        """One Autopilot query: the reply text, stripped, unchecked. Re-asked once after a no-reply
        or a busy code. ForbiddenCommand for a tag outside the catalogue."""
        check_query(tag)
        self._take("query", deadline)
        try:
            return self._read_locked(tag, check=False, deadline=deadline)
        finally:
            self._gate.release()

    def read(self, tag, *, deadline=None):
        """query plus the catalogue check: the value (int, str or None)."""
        check_query(tag)
        self._take("read", deadline)
        try:
            return self._read_locked(tag, deadline=deadline)
        finally:
            self._gate.release()

    def read_twice(self, tag, *, deadline=None):
        """Two reads second_reading_s apart that must agree (BadReply otherwise). The deadline applies to
        each read (§9 item 2)."""
        a = self.read(tag, deadline=deadline)
        self.clock.sleep(self.timings.second_reading_s)
        b = self.read(tag, deadline=deadline)
        if a != b:
            self._add_nonsense()
            raise _two_answers(self.label, tag, a, b)
        return a

    def _wrong_printer(self, got):
        return WrongPrinter(Msg(N_("{printer} is not the expected printer: UID {got} answered instead of {expected}."),
                                {"printer": self.label, "got": str(got), "expected": self.uid}),
                            expected=self.uid, got=str(got))

    def check_uid(self, *, deadline=None):
        """WrongPrinter when the printer twice answers another UID. The deadline applies to each read."""
        a = self.read("HXUI", deadline=deadline)
        if a == self.uid:
            return
        self.clock.sleep(self.timings.second_reading_s)
        b = self.read("HXUI", deadline=deadline)
        if a == b:
            raise self._wrong_printer(a)
        self._add_nonsense()
        raise _two_answers(self.label, "HXUI", a, b)

    def command(self, cmd, *, deadline=None):
        """One write, never repeated. After idle_warmup_s of quiet a checked HXUI read goes first.
        "0" returns; another code is CommandRefused; no reply is NoReply (it may have been done). With a
        deadline, the write is sent only if it can still end by it, also after the warm-up read: PrinterBusy
        otherwise, nothing written (§9 item 2)."""
        check_write(cmd)
        self._take("command", deadline)
        try:
            quiet = self.last_request_at is None or \
                self.clock.monotonic() - self.last_request_at >= self.timings.idle_warmup_s
            if quiet:
                uid = self._read_locked("HXUI", deadline=deadline)
                if uid != self.uid:
                    raise self._wrong_printer(uid)
            try:
                self._check(deadline)
            except _Late:
                raise _gate_busy(self.label) from None
            try:
                text = self._ask(cmd.xml, cmd.tag)
            except NoReply:
                self._hold_after_no_reply()
                raise NoReply(Msg(N_("No reply from {printer} to {command}. It may or may not have been done."),
                                  {"printer": self.label, "command": cmd.tag})) from None
            if re.split(r"[\s,;]", text, maxsplit=1)[0] == "0":
                return      # done; anything after the 0 is the manual's "second value"
            if _INT.fullmatch(text):
                if text in BUSY_REPLIES:
                    self._add_busy()
                raise CommandRefused(Msg(N_("{printer} refused {command} (code {code})."),
                                         {"printer": self.label, "command": cmd.tag, "code": int(text)}),
                                     tag=cmd.tag, code=int(text))
            self._add_nonsense()
            raise _bad(self.label, cmd.tag, text)
        finally:
            self._gate.release()

    def probe(self):
        """A bare TCP connect and close under the gate, nothing sent: True when it connected, False when
        it didn't, None at once when the gate was busy (the caller counts nothing)."""
        if not self._gate.acquire(blocking=False):
            return None
        try:
            try:
                with socket.create_connection((self.host, self.port), timeout=self.timings.http_timeout_s):
                    pass
                return True
            except OSError:
                return False
        finally:
            self._gate.release()

    # ---- web

    def _login_locked(self, deadline=None):
        md5 = password_md5(self._token)
        request = login_request(self.host, self.port, md5)
        reply = None
        for attempt in (1, 2):
            self._check(deadline)
            try:
                try:
                    reply = http_exchange(self.host, self.port, request, timeout_s=self.timings.http_timeout_s,
                                          max_body=MAX_LOGIN_REPLY, label=self.label, allow_cut_short=True,
                                          gunzip=False)
                finally:
                    self.last_request_at = self.clock.monotonic()
                break
            except NoReply:
                if attempt == 2:
                    self._hold_after_no_reply(deadline)
                    raise
                self._pause(deadline, no_reply=True)
            except BadReply:
                # §2.4 and §10.1 rule 3: a login reply that isn't a 200 with both cookies is AuthFailed,
                # broken framing included, so the poller shows login_failed (red) instead of dropping
                # the cycle. It still counts as nonsense (§13 item 7).
                self._cookies = {}
                self._add_nonsense()
                raise _refused_password(self.label) from None
        access, auth = f"Access_token_{self.uid}", f"Auth_token_{self.uid}"
        cookies = reply.cookies()
        if reply.status != 200 or access not in cookies or auth not in cookies:
            self._cookies = {}
            raise _refused_password(self.label)
        self._cookies = {access: cookies[access], auth: cookies[auth]}
        if self._logged_in:
            self.relogins += 1
        self._logged_in = True

    def login(self, *, deadline=None):
        """The web login (it changes nothing on the printer). Raises AuthFailed. With a deadline it never
        waits for this PC's gate past the latest start that lets both attempts end by it, and sends nothing
        that couldn't end by it: PrinterBusy instead (§9 item 2)."""
        self._take("login", deadline)
        try:
            self._login_locked(deadline)
        except _Late:
            raise _gate_busy(self.label) from None
        finally:
            self._gate.release()

    @staticmethod
    def _login_page(status, body):
        # The session has gone: 401, or 200 with the login page
        if status == 401:
            return True
        start = body.lstrip()[:16].lower()
        return status == 200 and (start.startswith(b"<!doctype") or start.startswith(b"<html"))

    def _web_reply(self, status, body, xid):
        """The class of one Load or repeat reply, counted (§10.5, §13)."""
        if status != 200:
            self._add_nonsense()
            return "odd"
        kind = classify_reply(body, tn=self.tn, xid=xid)
        if kind in ("foreign", "odd"):
            self._add_nonsense()
        elif kind == "busy":
            self._add_busy()
        return kind

    def _relogin(self, relogged, deadline):
        if relogged:
            raise _refused_password(self.label)     # a second 401 in one call
        self._cookies = {}
        self._login_locked(deadline)

    def load_slot(self, slot, *, deadline=None):
        """The printer's own copy of slot 0..10 (0 = printing now). Logs in when needed, sends a Load,
        then sme_repeat until the reply carries this request's TN and XID. WebReadFailed after
        web_repeat_max repeats or web_repeat_window_s; with a deadline, no exchange starts that
        couldn't end by it."""
        if isinstance(slot, bool) or not isinstance(slot, int) or not 0 <= slot <= 10:
            raise ValueError(f"no slot {slot!r}")
        self._take("load_slot", deadline, late=lambda: _not_in_time(self.label, slot))
        try:
            body, xid = self._load_locked(slot, deadline)
            return self._finish_load(body, slot, xid)
        except _Late:
            raise _not_in_time(self.label, slot) from None
        finally:
            self._gate.release()

    def _load_locked(self, slot, deadline):
        t = self.timings
        if not self._cookies:
            self._login_locked(deadline)
        relogged = False
        loads = 0
        while True:
            # A Load, with a new XID; after logging in again, with a fresh repeat count and window
            self._check(deadline)
            self.xid += 1
            xid = self.xid
            started = self.clock.monotonic()
            loads += 1
            request = sme_request(self.host, self.port, self._cookies, repeat=False,
                                  body=load_body(self.tn, xid, slot))
            try:
                status, body = self._exchange(request, MAX_SME_REPLY)
            except NoReply:
                if loads == 1:
                    self._pause(deadline, no_reply=True)
                    continue
                self._hold_after_no_reply(deadline)
                raise
            except BadReply:
                status, body = 0, b""           # too large or broken: odd
            if self._login_page(status, body):
                self._relogin(relogged, deadline)
                relogged = True
                continue
            if self._web_reply(status, body, xid) == "match":
                return body, xid
            repeats = 0
            no_reply = False
            while True:
                # After a repeat with no reply, this gap is also the hold of §8.1
                self._pause(deadline, no_reply=no_reply)
                if repeats >= t.web_repeat_max or self.clock.monotonic() - started >= t.web_repeat_window_s:
                    raise _not_in_time(self.label, slot)
                repeats += 1
                no_reply = False
                self._check(deadline)
                request = sme_request(self.host, self.port, self._cookies, repeat=True, body=_REPEAT_BODY)
                try:
                    status, body = self._exchange(request, MAX_SME_REPLY)
                except NoReply:
                    no_reply = True
                    continue                    # counts as one of the repeats
                except BadReply:
                    status, body = 0, b""
                if self._login_page(status, body):
                    self._relogin(relogged, deadline)
                    relogged = True
                    break                       # back to a new Load
                if self._web_reply(status, body, xid) == "match":
                    return body, xid

    def _finish_load(self, body, slot, xid):
        try:
            reply = parse_sme_reply(body)
        except WebReadFailed as e:
            self._add_nonsense()
            raise _relabel(e, self.label) from None
        design = reply.design
        if design is None or reply.msno in (None, 0):
            self._add_nonsense()
            raise _unrecognised(self.label)
        if (slot != 0 and reply.msno != slot) or (slot == 0 and not 1 <= reply.msno <= 10):
            self._add_nonsense()
            raise WebReadFailed(Msg(N_("{printer} sent back slot {got} instead of slot {slot}."),
                                    {"printer": self.label, "got": reply.msno, "slot": slot}))
        design.read_at = self.clock.time()
        design.tn = self.tn
        design.xid = xid
        return design

    def _file_locked(self, path, tag, deadline):
        """The body of one small web file, or None for a 404."""
        if not self._cookies:
            self._login_locked(deadline)
        relogged = False
        no_replies = 0
        while True:
            self._check(deadline)
            try:
                status, body = self._exchange(file_request(self.host, self.port, self._cookies, path), MAX_FILE_REPLY)
            except NoReply:
                no_replies += 1
                if no_replies == 1:
                    self._pause(deadline, no_reply=True)
                    continue
                self._hold_after_no_reply(deadline)
                raise
            except BadReply as e:
                self._add_nonsense()
                raise _bad(self.label, tag, e.reply) from None
            if self._login_page(status, body):
                self._relogin(relogged, deadline)
                relogged = True
                continue
            if status == 404:
                return None
            if status != 200:
                self._add_nonsense()
                raise _bad(self.label, tag, f"HTTP {status}")
            return body

    def fonts(self, *, deadline=None):
        """The printer's font list from GET /sfl.xml (OPEN: the source). BadReply when it can't be had."""
        self._take("fonts", deadline, late=lambda: _no_fonts(self.label))
        try:
            body = self._file_locked(FONT_FILE, "FONTS", deadline)
        except _Late:
            raise _no_fonts(self.label) from None
        finally:
            self._gate.release()
        if body is None:
            raise _no_fonts(self.label, "HTTP 404")
        try:
            return parse_fonts(body)
        except BadReply as e:
            raise _no_fonts(self.label, e.reply) from None

    def firmware(self, *, deadline=None):
        """The firmware version from GET /idn.xml (OPEN), or None when the file or the tag is missing."""
        try:
            self._take("firmware", deadline, late=_Late)
        except _Late:
            return None
        try:
            body = self._file_locked(IDENTITY_FILE, "FIRMWARE", deadline)
        except _Late:
            return None
        finally:
            self._gate.release()
        return None if body is None else parse_firmware(body)


# --------------------------------------------------------------------------
# ClientPool
# --------------------------------------------------------------------------

class ClientPool:
    """One PrinterClient per printer record, rebuilt when host, port, uid or token change."""

    def __init__(self, *, key, clock=SYSTEM_CLOCK, gates=PROCESS_GATES, timings=TIMINGS):
        self._key = key
        self.clock = clock
        self.gates = gates
        self.timings = timings
        self._lock = threading.Lock()
        self._clients = {}       # printer id -> ((host, port, uid, stored token), client)

    def client(self, printer):
        """Raises AuthFailed when no password is saved or the saved one can't be read."""
        wanted = (printer.host, printer.port, printer.uid, printer.token)
        with self._lock:
            cached = self._clients.get(printer.id)
            if cached is not None and cached[0] == wanted:
                client = cached[1]
            else:
                if printer.token == "":
                    raise AuthFailed(Msg(N_("No password is saved for {printer}."), {"printer": printer.name}))
                try:
                    token = unscramble(printer.token, self._key)
                except AuthFailed as e:
                    raise AuthFailed(Msg(N_("{printer}: {problem}"),
                                         {"printer": printer.name, "problem": e.msg})) from None
                client = PrinterClient(printer.host, printer.uid, token, port=printer.port, clock=self.clock,
                                       gates=self.gates, timings=self.timings)
                self._clients[printer.id] = (wanted, client)
            client.label = printer.name
            return client
