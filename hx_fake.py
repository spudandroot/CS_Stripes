"""Fake MapleJet Hx printers, a fake time server and a seeded fake share, for tests and manual runs.

Never imported by the app. FakePrinter is a real HTTP server on 127.0.0.1 that answers
Autopilot commands, the web login, Load and sme_repeat (with the printer's one shared
reply buffer), and has each of the real printer's bad habits behind a switch: mixed-up
and busy replies when requests overlap, the first request after a quiet spell dropped,
the slow editor page, codes in 200 replies, sizes read back one lower. Anything Stripes
must never do is recorded in `violations`.

FakeSntp answers SNTP on UDP. FakeWorld puts fakes and a temp share together, seeded
with example records (Example Co, Line 1 Bagger 1, ...).

    python hx_fake.py [--printers N] [--encoder] [--mixup]

runs them until Ctrl+C and prints the STRIPES_V2 and STRIPES_HX_* values to start
`python stripes.py` against them.
"""

from __future__ import annotations

import argparse
import base64
import calendar
import gzip
import hashlib
import http.server
import os
import random
import re
import shlex
import shutil
import socket
import socketserver
import struct
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
from datetime import datetime, timedelta

import hx_protocol
import hx_share
import hx_time
from hx_core import SYSTEM_CLOCK
from hx_model import (ALLOWED_WRITE_TAGS, FORBIDDEN_QUERY_TAGS, STANDARD_FONTS, TIMINGS, Company, Country,
                      DesignItem, Film, Identity, Ink, Line, Machine, MessageDesign, Printer, PrintText,
                      PrintTextItem, Product, new_id)
from hx_protocol import (EDITOR_PATH, PRINTER_WIDE_WRITES, QUERY_SPECS, SME_PATH, SME_REPEAT_PATH, WEB_FILES,
                         decode_text, make_token, release_key, scramble)
from hx_time import local_utc_offset

# What a message gets for every print setting not sent (seen), in the printer's order
FAKE_DEFAULT_SETTINGS = (
    ("ROTAT", "0"), ("BOLD", "1"), ("WDTH", "1"), ("QUANT", "1"), ("DELAY", "0"), ("QDELAY", "0"),
    ("AUTREP", "0"), ("REVRS", "0"), ("DRAFT", "0"), ("MIRR", "0"), ("SLANT", "0"), ("ANG", "30"),
    ("SWDLY", "0"), ("SWSPD", "250"), ("WPDLY", "0"), ("PRNSPD", "1000"), ("ENCST", "0"), ("RESMOD", "0"),
    ("DLYMM", "0"), ("QDLYMM", "0"), ("ENCO", "0"), ("RESO", "640"), ("RESNAM", "UltraFine"),
    ("ENCSCL", "100"),
)
RESOLUTION_NAMES = ("UltraFine", "Fine", "Normal", "Draft")     # RESNAM for RESMOD 0..3 (OPEN: 1 to 3)

LOGIN_HTML = (b'<!DOCTYPE html><html><body><form method="post"><input type="password" '
              b'name="pass"></form><p>V.72.5</p></body></html>')
MAIN_HTML = b'<!DOCTYPE html><html><body><p>Hx</p></body></html>'

# The example records' group SIDs (FakeWorld)
ADMIN_SID = "S-1-5-21-1000-2000-3000-1101"
SENDER_SID = "S-1-5-21-1000-2000-3000-1102"

_EPOCH = datetime(1970, 1, 1)
_MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
# A crude font metric (character width and height per size step) that gives the capture's BOUNDs
_CW = {1: 13 / 3, 2: 3.5, 3: 3.0, 4: 3.5, 5: 4.0, 6: 4.0}
_CH = {1: 23 / 3, 2: 9.0, 3: 9.0, 4: 9.0, 5: 9.0, 6: 9.0}
# How many fields come before the common ones (verbose rendering puts a blank line there)
_SPECIFIC = {"CTDAT": 11, "CLOT": 1, "CCONT": 5}
_WEB_KINDS = ("login", "sme", "sme_repeat", "get")
_AUTH = re.compile(r"\s*<Cookie><Auth_token_(\d+)>([^<]*)</Auth_token_\1></Cookie><APCMD>(.*)</APCMD>\s*", re.DOTALL)
_LOAD = re.compile(r"<SM><TYPE>(\w+)</TYPE><TN>(\d*)</TN><XID>(\d*)</XID><MSNO>(\d+)</MSNO></SM>")
_CHILD = re.compile(r"\s*<([A-Za-z][A-Za-z0-9_]*)>([^<]*)</\1>")
_EDITOR_PIECE = 65_536
_EDITOR = object()      # _compute's answer for the editor page, which is sent in pieces


# --------------------------------------------------------------------------
# The preview picture
# --------------------------------------------------------------------------

def _draw(rects, width, height):
    """(the BMP bytes, its count of black pixels)."""
    row_bytes = (width + 31) // 32 * 4
    rows = [bytearray(b"\xff" * row_bytes) for _ in range(height)]     # white; stored bottom-up
    for x1, y1, x2, y2 in rects:
        x1, x2 = max(0, x1), min(width, x2)
        if x1 >= x2:
            continue
        b1, b2 = x1 >> 3, (x2 - 1) >> 3
        left = 0xFF >> (x1 & 7)                   # bits x1.. of the first byte
        right = (0xFF << (7 - ((x2 - 1) & 7))) & 0xFF   # bits ..x2-1 of the last byte
        for y in range(max(0, y1), min(height, y2)):
            row = rows[height - 1 - y]
            if b1 == b2:
                row[b1] &= ~(left & right) & 0xFF
            else:
                row[b1] &= ~left & 0xFF
                row[b2] &= ~right & 0xFF
                row[b1 + 1:b2] = bytes(b2 - b1 - 1)
    pixels = b"".join(rows)
    black = len(pixels) * 8 - int.from_bytes(pixels, "big").bit_count()
    header = struct.pack("<2sIHHI", b"BM", 62 + len(pixels), 0, 0, 62)
    info = struct.pack("<IiiHHIIiiII", 40, width, height, 1, 1, 0, len(pixels), 0, 0, 0, 0)
    return header + info + b"\x00\x00\x00\x00\xff\xff\xff\x00" + pixels, black


def bmp_1bit(rects, width=1024, height=160):
    """A white 1-bit BMP with black rectangles (x1, y1, x2, y2), top-left origin, x2 and y2
    exclusive; the header the real printer sends. Rectangles are clipped, overlaps count once."""
    return _draw(rects, width, height)[0]


def _render_date(frmt, when, days=0, months=0, years=0):
    """The printer's date format: Y, y, M, m, D, H, U, E are fields, anything else is copied.
    Offsets: years, then months (the day kept within the month), then days."""
    def shifted(d, add_months):
        total = d.year * 12 + d.month - 1 + add_months
        year, month = divmod(total, 12)
        month += 1
        return d.replace(year=year, month=month, day=min(d.day, calendar.monthrange(year, month)[1]))
    d = shifted(shifted(when, 12 * years), months) + timedelta(days=days)
    fields = {"Y": f"{d.year:04d}", "y": f"{d.year % 100:02d}", "M": f"{d.month:02d}",
              "m": _MONTHS[d.month - 1], "D": f"{d.day:02d}", "H": f"{d.hour:02d}", "U": f"{d.minute:02d}",
              "E": f"{d.second:02d}"}
    return "".join(fields.get(c, c) for c in frmt)


def _number(text, default=0):
    return int(text) if re.fullmatch(r"-?\d{1,10}", text or "") else default


def _gzip(data):
    return gzip.compress(data, mtime=0)


# --------------------------------------------------------------------------
# The HTTP server
# --------------------------------------------------------------------------

class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self):
        # HTTPServer.server_bind looks the host name up; 127.0.0.1 needs no lookup
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class _Handler(http.server.BaseHTTPRequestHandler):
    def handle(self):
        try:
            self.server.fake._handle(self.connection)
        except Exception:
            traceback.print_exc()

    def finish(self):
        try:
            super().finish()
        except OSError:
            pass


class _Request:
    """One request as it arrived, and what it asks for."""

    def __init__(self, raw):
        self.raw = raw
        self.kind, self.tag, self.method, self.path = "bad", "", "", ""
        self.headers = {}
        self.body = b""
        self.uid = None             # Autopilot: the UID and token it carries, the command
        self.token = None
        self.query = False
        self.inner = ""
        self.parsed = False
        self.extra = False          # text after the first command
        head, sep, body = raw.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        m = re.fullmatch(r"([A-Z]+) (\S+) HTTP/1\.[01]", lines[0])
        if not sep or not m:
            return
        self.method, self.path = m.groups()
        for line in lines[1:]:
            name, colon, value = line.partition(":")
            if colon:
                self.headers.setdefault(name.strip().lower(), value.strip())
        self.body = body
        if self.method == "POST" and self.path == "/":
            if body.startswith(b"Type=LOG"):
                self.kind, self.tag = "login", "LOG"
            else:
                self.kind = "autopilot"
                self._autopilot(decode_text(body))
        elif self.method == "POST" and self.path == SME_PATH:
            self.kind = "sme"
            m = re.search(rb"<TYPE>(\w*)</TYPE>", body)
            self.tag = m.group(1).decode("ascii") if m else ""
        elif self.method == "POST" and self.path == SME_REPEAT_PATH:
            self.kind, self.tag = "sme_repeat", "RepeatRequest"
        elif self.method == "GET":
            self.kind, self.tag = "get", self.path
        else:
            self.tag = self.path

    def _autopilot(self, text):
        m = _AUTH.fullmatch(text)
        if not m:
            return
        self.uid, self.token, command = int(m.group(1)), m.group(2), m.group(3)
        q = re.match(r"\s*<([A-Za-z][A-Za-z0-9_]*)\s*/>", command)
        w = None if q else re.match(r"\s*<([A-Za-z][A-Za-z0-9_]*)>(.*?)</\1>", command, re.DOTALL)
        found = q or w
        if not found:
            return
        self.tag = found.group(1)
        self.query = q is not None
        self.inner = "" if q else w.group(2)
        self.parsed = True
        self.extra = command[found.end():].strip() != ""

    def cookies(self):
        found = {}
        for part in self.headers.get("cookie", "").split(";"):
            name, eq, value = part.partition("=")
            if eq:
                found[name.strip()] = value.strip()
        return found


def _response(status, headers, body):
    reason = {200: "OK", 400: "Bad Request", 401: "Unauthorized", 404: "Not Found"}.get(status, "Error")
    head = f"HTTP/1.1 {status} {reason}\r\n" + "".join(f"{k}: {v}\r\n" for k, v in headers) + "\r\n"
    return head.encode("latin-1") + body


_SERVER = (("Server", "MapJet/v1.1"), ("Connection", "close"))


# --------------------------------------------------------------------------
# FakePrinter
# --------------------------------------------------------------------------

class FakePrinter:
    """An Hx printer on 127.0.0.1: Autopilot on POST /, the web login, Load and sme_repeat with one
    shared reply buffer, and the real printer's bad habits, each switchable."""

    DROP = object()     # override result: carry out a write, then close without a reply
    SILENT = object()   # override result: hold the connection and never answer

    def __init__(self, *, uid=1109, password="123321", placement="delay", fonts=STANDARD_FONTS,
                 firmware="69.75.10", clock=SYSTEM_CLOCK, latency_s=0.0, mixup=False, busy_on_overlap=True,
                 drop_after_idle_s=None, editor_page_bytes=2_300_000, editor_stall_s=5.0, seed=0,
                 offset_fn=local_utc_offset):
        self.host = "127.0.0.1"
        self.port = 0
        self.lock = threading.Lock()
        # Knobs: plain attributes, read under self.lock by the handlers
        self.uid = uid
        self.password = password
        self.placement = placement
        self.fonts = tuple(fonts)
        self.firmware = firmware
        self.clock = clock
        self.latency_s = latency_s
        self.mixup = mixup
        self.busy_on_overlap = busy_on_overlap
        self.drop_after_idle_s = drop_after_idle_s
        self.editor_page_bytes = editor_page_bytes
        self.editor_stall_s = editor_stall_s
        self.offset_fn = offset_fn
        self.base_offset_s = offset_fn(clock.time())   # fixed: the printer never follows daylight saving
        self.hx_name = " Printer 1"          # with the real printer's leading space
        self.mixup_p = 0.5
        self.drop_after_idle_p = 1.0
        self.overload_at = 8
        self.overload_pause_s = 1.0
        self.multi_value_hang_s = 4.0
        self.web_latency_s = 0.0
        self.web_result_delay_s = 0.0
        self.web_first_reply_fresh = False
        self.web_reply_hook = None           # bytes -> bytes; runs under self.lock, so it mustn't call the fake
        self.single_web_session = False
        self.write_echo = False
        self.gzip_files = False
        self.files = {}                      # overrides for the small files; None answers 404
        # Printer state
        encoder = placement == "encoder"
        self.slots = {1: MessageDesign(
            slot=1, name="", empty=False, height=160,
            items=[DesignItem(tag="CTDAT", pairs=[
                ("OBJID", "1"), ("POS", "0,0"), ("PREF", "Packed: "), ("FRMT", "M/D/y"), ("DAY", ""),
                ("MON", ""), ("YEAR", ""), ("POSF", ""), ("FONTIND", "1"), ("SIZE", "4"), ("MARG", "0,0"),
                ("RP", "0"), ("LANG", "0")])],
            settings={"ROTAT": "0", "MIRR": "0", "REVRS": "0", "AUTREP": "0", "DELAY": "0", "DLYMM": "0",
                      "PRNSPD": "2500", "BOLD": "1", "WDTH": "1", "QUANT": "1", "QDELAY": "0", "QDLYMM": "0",
                      "RESMOD": "0", "RESNAM": "UltraFine", "ENCST": "1" if encoder else "0", "ENCSCL": "100"})}
        self.slot_style = {1: "compact"}     # "verbose" (made by Autopilot) | "compact" (another tool)
        self.current_slot = 1
        self.printing = True
        self.counters = {"PMCNT": 100000, "MSCNT": 0}
        self.ink_name = "Example Black"
        self.ink_level = 1
        self.head_temp_c = 25
        self.eco = 0
        self.clock_offset_s = 0.0
        self.encoder = {"ENCST": 1 if encoder else 0, "ENWHD": 56, "ENPPR": 5000, "ENTPM": 1}
        self.cartridge = {"PLDEL": 1210, "MNPL": 450, "PRPL": 220, "PLVO": 9500, "PLWR": 27, "NZRO": 2}
        self.lots = {i: "" for i in range(1, 10)}
        self.rollover = None
        self.sessions = set()                # valid (Access_token, Auth_token) values
        # Observation
        self.requests = []
        self.writes = []
        self.violations = []
        self.printed = []
        self.max_in_flight = 0
        self.in_flight = 0
        # Internals
        self._rng = random.Random(seed)
        self._design = None                  # the message being built since CRTMSG
        self._next_objid = 1
        self._overrides = {}
        self._silent = False
        self._silent_epoch = 0               # go_silent(False) releases every silent handler
        self._overload_until = 0.0           # real time
        self._overload_epoch = 0
        self._last_activity = clock.monotonic()
        self._last_autopilot = None          # (request, reply) most recently computed, for mixups
        self._pending = None                 # (result, due) of a delayed web result
        self._editors = 0
        self._editor_idle = threading.Event()
        self._editor_idle.set()
        self._stopping = threading.Event()
        self._server = None
        self._thread = None
        self.buffer = self._save_result(1)   # the one shared SME reply buffer

    # ---- clock

    def printer_clock(self):
        """The printer's own clock: naive, and it never follows daylight saving."""
        return _EPOCH + timedelta(seconds=self.clock.time() + self.base_offset_s + self.clock_offset_s)

    # ---- server

    def start(self):
        self._server = _Server(("127.0.0.1", 0), _Handler)
        self._server.fake = self
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.05},
                                        name=f"hx-fake-{self.port}", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        """Like switching the printer off: new connections are refused."""
        if self._server is None:
            return
        self._stopping.set()
        self._editor_idle.set()
        server, self._server = self._server, None
        server.shutdown()
        server.server_close()
        self._thread.join(timeout=2.0)

    # ---- controls

    def print_bags(self, n):
        """As if the bagger triggered n prints: only while printing, with a message that has items."""
        with self.lock:
            design = self.slots.get(self.current_slot)
            if not self.printing or design is None or not design.items:
                return
            encoder = design.settings.get("ENCST", str(self.encoder["ENCST"])) == "1"
            for _ in range(n):
                self.counters["PMCNT"] += 1
                self.counters["MSCNT"] += 1
                self.printed.append({
                    "slot": self.current_slot, "name": design.name, "encoder": encoder,
                    "position_mm": _number(design.settings.get("DLYMM", "0")) if encoder else None,
                    "delay_us": _number(design.settings.get("DELAY", "0")) if not encoder else None,
                    "at": self.clock.time()})

    def go_silent(self, silent=True):
        """Stop answering: connections are held until the client gives up (a Wi-Fi drop)."""
        with self.lock:
            self._silent = silent
            if not silent:
                self._silent_epoch += 1

    def override(self, tag, reply):
        """Answer this Autopilot tag with reply: a string, or a callable giving a string, None (behave
        normally), DROP or SILENT. None removes the override."""
        with self.lock:
            if reply is None:
                self._overrides.pop(tag, None)
            else:
                self._overrides[tag] = reply

    def expire_web_session(self):
        with self.lock:
            self.sessions.clear()

    def leave_in_buffer(self, body):
        """What another client's request left in the shared reply buffer."""
        with self.lock:
            self.buffer = bytes(body)

    def set_password(self, password):
        with self.lock:
            self.password = password
            self.sessions.clear()

    def reset_observations(self):
        with self.lock:
            self.requests.clear()
            self.writes.clear()
            self.violations.clear()
            self.printed.clear()
            self.max_in_flight = 0

    def printer_record(self, **fields):
        """A Printer pointing at this fake, its token scrambled with hx_protocol.release_key()."""
        if not self.port:
            raise RuntimeError("start() the fake printer first")
        values = dict(id=new_id("pr"), name="Printer 1", host=self.host, port=self.port, uid=self.uid,
                      token=scramble(make_token(self.password), release_key()))
        values.update(fields)
        return Printer(**values)

    # ---- one request (§15.2)

    def _handle(self, conn):
        raw = self._read(conn)
        if raw is None:
            return          # a bare connect (the reachability probe): not a request
        req = _Request(raw)
        editor = req.kind == "get" and req.path == EDITOR_PATH
        with self.lock:
            record = {"t": self.clock.monotonic(), "kind": req.kind, "tag": req.tag, "body": req.body,
                      "raw": raw, "reply": None, "status": None}
            self.requests.append(record)
            if editor:
                self._editors += 1
                self._editor_idle.clear()
            authed = (req.kind == "autopilot" and req.uid == self.uid and req.token == make_token(self.password))
            override = None
            if authed and req.parsed:
                # Every command received counts, also one later answered busy, mixed up or not at all
                if req.extra:
                    self.violations.append("two commands in one request")
                if not req.query:
                    self.writes.append(req.tag)
                self._note_violations(req.tag, req.query, req.inner)
                override = self._overrides.get(req.tag)
        try:
            # A callable override may do slow work, so it runs outside the lock
            if callable(override):
                override = override()
            self._serve(conn, req, record, authed, override, editor)
        finally:
            if editor:
                with self.lock:
                    self._editors -= 1
                    if not self._editors:
                        self._editor_idle.set()

    def _serve(self, conn, req, record, authed, override, editor):
        if not editor:
            while not self._editor_idle.wait(0.05) and not self._stopping.is_set():
                pass                        # the editor page stalls every other request
        with self.lock:
            silent = self._silent or override is self.SILENT
            epoch = self._silent_epoch
        if silent:
            self._hold(conn, epoch)
            return
        with self.lock:
            if time.monotonic() < self._overload_until:
                return                      # still recovering from an overload: no answer
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            overlapping = self.in_flight >= 2
            if overlapping:
                self.violations.append("two requests in flight")
            if self.in_flight >= self.overload_at:
                self._overload_until = time.monotonic() + self.overload_pause_s
                self._overload_epoch += 1
                self.in_flight -= 1
                return
            overload_epoch = self._overload_epoch
            now = self.clock.monotonic()
            drop = (self.drop_after_idle_s is not None and now - self._last_activity >= self.drop_after_idle_s
                    and self._rng.random() < self.drop_after_idle_p)
            self._last_activity = now
            if drop:
                self.in_flight -= 1
                return
            wait = self.latency_s + (self.web_latency_s if req.kind in _WEB_KINDS else 0.0)
            if (authed and req.parsed and req.query and req.tag in FORBIDDEN_QUERY_TAGS
                    and override is None):
                wait += self.multi_value_hang_s
        self._stopping.wait(wait)
        with self.lock:
            if req.kind == "autopilot" and overlapping:
                reply = None
                if (self.mixup and self._last_autopilot is not None and self._last_autopilot[0] is not record
                        and self._rng.random() < self.mixup_p):
                    reply = self._last_autopilot[1]
                elif self.busy_on_overlap:
                    reply = self._text("7")
                if reply is None:
                    reply = self._compute(req, record, authed, override)
            else:
                reply = self._compute(req, record, authed, override)
            self.in_flight -= 1             # before writing: a client holding its reply can't overlap
            if self._overload_epoch != overload_epoch or reply is self.DROP:
                return
            if reply is _EDITOR:
                record["status"] = 200
            else:
                record["status"] = int(reply[9:12])
                record["reply"] = reply.partition(b"\r\n\r\n")[2]
        try:
            if reply is _EDITOR:
                self._send_editor(conn)
            else:
                conn.sendall(reply)
        except OSError:
            pass

    @staticmethod
    def _read(conn):
        """The raw request: the head up to the blank line, then Content-Length bytes."""
        conn.settimeout(10.0)
        data = b""
        try:
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(65536)
                if not chunk:
                    return data or None
                data += chunk
                if len(data) > 1_048_576:
                    return data
            head = data.partition(b"\r\n\r\n")[0]
            m = re.search(rb"(?im)^content-length:[ \t]*(\d{1,9})[ \t]*$", head)
            total = len(head) + 4 + (int(m.group(1)) if m else 0)
            while len(data) < total:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                data += chunk
        except OSError:
            return data or None
        return data

    def _hold(self, conn, epoch):
        """Silent: hold the connection until the client gives up, go_silent(False) or stop()."""
        conn.settimeout(0.02)
        while not self._stopping.is_set():
            with self.lock:
                if self._silent_epoch != epoch:
                    return
            try:
                if not conn.recv(4096):
                    return          # the client closed
            except socket.timeout:
                continue
            except OSError:
                return

    def _send_editor(self, conn):
        size = self.editor_page_bytes
        conn.sendall(_response(200, _SERVER + (("Content-Type", "text/html"), ("Content-Length", str(size))), b""))
        pieces = max(1, -(-size // _EDITOR_PIECE))
        gap = self.editor_stall_s / pieces
        page = (b"<!DOCTYPE html><html><body>" + b"x" * max(0, size - 41) + b"</body></html>")[:size]
        for i in range(pieces):
            if self._stopping.wait(gap):
                return
            conn.sendall(page[i * _EDITOR_PIECE:(i + 1) * _EDITOR_PIECE])

    # ---- replies (lock held)

    @staticmethod
    def _text(text):
        body = text.encode("utf-8")
        return _response(200, _SERVER + (("Content-Length", str(len(body))), ("Content-Type", "text/plain")), body)

    @staticmethod
    def _html(status, page, cookies=()):
        gz = _gzip(page)
        headers = _SERVER + (("Content-Type", "text/html"), ("Content-Encoding", "gzip")) + tuple(
            ("Set-Cookie", c) for c in cookies) + (("Content-Length", str(len(gz))),)
        return _response(status, headers, gz)

    @staticmethod
    def _xml(body, gzipped=False):
        if gzipped:
            body = _gzip(body)
        headers = _SERVER + (("Content-Length", str(len(body))), ("Content-Type", "application/xml"))
        if gzipped:
            headers += (("Content-Encoding", "gzip"),)
        return _response(200, headers, body)

    @staticmethod
    def _not_found():
        return _response(404, _SERVER + (("Content-Length", "0"),), b"")

    def _compute(self, req, record, authed, override):
        if req.kind == "autopilot":
            if not authed:
                return self._html(401, LOGIN_HTML)
            if not req.parsed:
                reply = self._text("2")
            else:
                reply = self._autopilot(req, override)
            if reply is not self.DROP:
                self._last_autopilot = (record, reply)
            return reply
        if req.kind == "login":
            return self._login(req)
        if req.kind in ("sme", "sme_repeat") or (req.kind == "get" and req.path in WEB_FILES + ("/",)):
            pair = (req.cookies().get(f"Access_token_{self.uid}"), req.cookies().get(f"Auth_token_{self.uid}"))
            if pair not in self.sessions:
                return self._html(401, LOGIN_HTML)
            if req.kind == "sme":
                return self._xml(self._sme_load(req.body))
            if req.kind == "sme_repeat":
                if req.body != b"<SM>RepeatRequest</SM>":
                    self.violations.append("bad SME request")
                self._promote()
                return self._xml(self.buffer)
            if req.path == "/":
                return self._html(200, MAIN_HTML)
            return self._file(req.path)
        if req.kind == "get" and req.path == EDITOR_PATH:
            self.violations.append("editor page requested")
            return _EDITOR
        self.violations.append(f"web file requested {req.path}")
        return self._not_found()

    def _login(self, req):
        form = urllib.parse.parse_qs(req.body.decode("latin-1"))
        if form.get("pass", [""])[0] != hashlib.md5(self.password.encode("utf-8")).hexdigest():
            return self._html(200, LOGIN_HTML)          # a wrong password: the login page again (OPEN)
        session = (f"{self._rng.getrandbits(128):032x}", f"{self._rng.getrandbits(128):032x}")
        if self.single_web_session:
            self.sessions = {session}
        else:
            self.sessions.add(session)
        return self._html(200, MAIN_HTML, (f"Access_token_{self.uid}={session[0]}",
                                           f"Auth_token_{self.uid}={session[1]}"))

    def _file(self, path):
        if path in self.files:
            body = self.files[path]
            if body is None:
                return self._not_found()
        elif path == "/sfl.xml":
            body = ("<SFL>\r\n" + "".join(f"<FON{i}>{name}</FON{i}>\r\n" for i, name in enumerate(self.fonts, 1))
                    + "</SFL>\r\n").encode("utf-8")
        else:
            body = f"<IDN>\r\n<HXUI>{self.uid}</HXUI>\r\n<FW>{self.firmware}</FW>\r\n</IDN>\r\n".encode("utf-8")
        return self._xml(body, gzipped=self.gzip_files)

    # ---- web read-back (§15.5)

    def _promote(self):
        if self._pending is not None and self.clock.monotonic() >= self._pending[1]:
            self.buffer = self._pending[0]
            self._pending = None

    def _sme_load(self, body):
        m = _LOAD.fullmatch(decode_text(body))
        if not m:
            self.violations.append("bad SME request")
            return self.buffer
        type_, tn, xid, msno = m.groups()
        if type_ != "Load":
            self.violations.append("bad SME request")      # Stripes sends no other TYPE
            if type_ != "Thumbnail":
                return self.buffer
        self._promote()
        answer = self.buffer
        slot = int(msno) or self.current_slot
        result = self._result(type_, tn, xid, slot)
        if self.web_reply_hook is not None:
            result = self.web_reply_hook(result)
        if self.web_first_reply_fresh:
            self.buffer = answer = result
        elif self.web_result_delay_s == 0:
            self.buffer = result
        else:
            self._pending = (result, self.clock.monotonic() + self.web_result_delay_s)
        return answer

    def _result(self, type_, tn, xid, slot):
        head = [b"<SM>", b"<TYPE>%s</TYPE>" % type_.encode(), b"<TN>%s</TN>" % tn.encode(),
                b"<XID>%s</XID>" % xid.encode(), b"<MSNO>%d</MSNO>" % slot]
        design = self.slots.get(slot)
        if design is None:
            return self._lines(head + [b"<PRV>"] + self._prv(0, "", bmp_1bit([])) + [b"</SM>"])
        items = [(item, self._rendered(item)) for item in design.items]
        picture, black = _draw([rect for _, (_, rect) in items], 1024, 160)
        prv = self._prv(black, design.name, picture)
        if type_ == "Thumbnail":
            return self._lines(head + [b"<PRV>"] + prv + [b"</SM>"])
        settings = [self._leaf(k, v) for k, v in design.settings.items()]
        if self.slot_style.get(slot, "verbose") == "compact":
            fcdoc = (b"<FCDOC>" + self._leaf("MSGNAM", design.name)
                     + b"".join(b"<%s>" % item.tag.encode() + b"".join(self._leaf(k, v) for k, v in pairs)
                                + b"</%s>" % item.tag.encode() for item, (pairs, _) in items)
                     + b"<CPPARM>" + b"".join(settings) + b"</CPPARM><H>160</H>")
            return self._lines(head + [fcdoc, b"</FCDOC><PRV>"] + prv + [b"</SM>"])
        lines = head + [b"<FCDOC>", b"<VER>1</VER>", b"<W>256</W>", b"<H>160</H>", self._leaf("MSGNAM", design.name)]
        for item, (pairs, _) in items:
            lines.append(b"<%s>" % item.tag.encode())
            specific = _SPECIFIC.get(item.tag, 0)
            for i, (k, v) in enumerate(pairs):
                if specific and i == specific:
                    lines.append(b"")
                lines.append(self._leaf(k, v))
            lines.append(b"</%s>" % item.tag.encode())
        lines += [b"<CPPARM>"] + settings + [b"</CPPARM>", b"<H>160</H>", b"</FCDOC><PRV>"] + prv + [b"</SM>"]
        return self._lines(lines)

    def _save_result(self, slot):
        """What SAVEMSG leaves in the buffer: TYPE Save, empty TN and XID, the slot's picture."""
        thumb = self._result("Thumbnail", "", "", slot)
        return thumb.replace(b"<TYPE>Thumbnail</TYPE>", b"<TYPE>Save</TYPE>", 1)

    @staticmethod
    def _lines(lines):
        return b"".join(line + b"\r\n" for line in lines)

    @staticmethod
    def _leaf(tag, value):
        # Values are UTF-8, except AMPM's single 0xFF byte
        try:
            data = value.encode("latin-1") if tag == "AMPM" else value.encode("utf-8")
        except UnicodeEncodeError:
            data = value.encode("utf-8")
        return b"<%s>%s</%s>" % (tag.encode(), data, tag.encode())

    @staticmethod
    def _prv(black, name, picture):
        uri = b"data:image/bmp;base64, " + base64.b64encode(picture)
        return [b"<BPIX>%d</BPIX>" % black, FakePrinter._leaf("MSGNAM", name), b"<BMP>" + uri + b"</BMP>", b"</PRV>"]

    def _item_text(self, item):
        """What an item prints now: its text, the date rendered on the printer clock, the lot."""
        if item.tag == "CTDAT":
            return _render_date(item.value("FRMT", ""), self.printer_clock(), days=_number(item.value("DAY")),
                                months=_number(item.value("MON")), years=_number(item.value("YEAR")))
        if item.tag == "CLOT":
            return self.lots.get(_number(item.value("INDX"), 1), "")
        if item.tag == "CCONT":
            return item.value("STRVAL", "")
        return item.value("TXT", "")

    def _rendered(self, item):
        """(the item's pairs with TXT and BOUND worked out now, its rectangle)."""
        text = self._item_text(item)
        m = re.fullmatch(r"(-?\d+),(-?\d+)", item.value("POS", "0,0") or "")
        x1, y1 = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
        font = _number(item.value("FONTIND"), 1)
        size = _number(item.value("SIZE"), 0) + 1        # stored one lower than sent
        chars = len(item.value("PREF", "") + text + item.value("POSF", ""))
        x2 = x1 + chars * round(_CW.get(font, 4.0) * size)
        y2 = y1 + round(_CH.get(font, 9.0) * size)
        pairs = []
        for k, v in item.pairs:
            if k == "TXT" and item.tag != "CTEXT":
                v = text
            elif k == "BOUND":
                v = f"{x1},{y1},{x2},{y2}"
            pairs.append((k, v))
        return pairs, (x1, y1, x2, y2)

    # ---- Autopilot (§15.3)

    def _autopilot(self, req, override):
        tag, inner = req.tag, req.inner
        if isinstance(override, str):
            if not req.query and re.split(r"[\s,;]", override, maxsplit=1)[0] == "0":
                self._write(tag, inner)
            return self._text(override)
        if override is self.DROP:
            if not req.query:
                self._write(tag, inner)
            return self.DROP
        return self._text(self._query(tag) if req.query else self._write(tag, inner))

    def _note_violations(self, tag, query, inner):
        if query:
            if tag in FORBIDDEN_QUERY_TAGS:
                self.violations.append(f"multi-value query {tag}")
            elif tag not in QUERY_SPECS:
                self.violations.append(f"query not in catalogue {tag}")
            return
        if tag in PRINTER_WIDE_WRITES:
            self.violations.append(f"printer-wide command {tag}")
        elif tag not in ALLOWED_WRITE_TAGS:
            self.violations.append(f"write not whitelisted {tag}")
        elif tag in ("SAVEMSG", "PRINTMSG") and re.fullmatch(r"\d{1,9}", inner.strip()):
            n = int(inner.strip())
            if n not in (9, 10):
                self.violations.append("write outside slots 9 and 10")
            if tag == "SAVEMSG" and n == self.current_slot and self.printing:
                self.violations.append("SAVEMSG to the printing slot")

    def _current(self, key, default):
        design = self.slots.get(self.current_slot)
        if design is not None and key in design.settings:
            return design.settings[key]
        return default

    def _query(self, tag):
        if tag in FORBIDDEN_QUERY_TAGS:
            return "6"      # after the hang
        if tag not in QUERY_SPECS:
            m = re.fullmatch(r"LOT([1-9])", tag)
            return self.lots[int(m.group(1))] if m else "-1"
        now = self.printer_clock()
        design = self.slots.get(self.current_slot)
        defaults = dict(FAKE_DEFAULT_SETTINGS)
        answers = {
            "HXUI": lambda: str(self.uid),
            "HXNA": lambda: self.hx_name,
            "PRIN": lambda: "1" if self.printing else "0",
            "MSGNAM": lambda: design.name if design is not None and design.name else "None",
            "MSCNT": lambda: str(self.counters["MSCNT"]),
            "PMCNT": lambda: str(self.counters["PMCNT"]),
            "DATE": lambda: f"{now.day:02d}/{now.month:02d}/{now.year:04d}",
            "TIME": lambda: f"{now.hour:02d}:{now.minute:02d}:{now.second:02d}",
            "INKT": lambda: self.ink_name or "None",
            "INKL": lambda: str(self.ink_level),
            "PHTP": lambda: str(self.head_temp_c),
            "ECOMOD": lambda: str(self.eco),
            "ENCST": lambda: self._current("ENCST", str(self.encoder["ENCST"])),
            "DELAY": lambda: str(_number(self._current("DELAY", "0")) // 1000),     # ms, like the printer
        }
        if tag in answers:
            return answers[tag]()
        if tag in self.encoder:
            return str(self.encoder[tag])
        if tag in self.cartridge:
            return str(self.cartridge[tag])
        return self._current(tag, defaults.get(tag, "-1"))

    def _children(self, inner, allowed):
        """The leaves of a block write as a dict, "2" when it doesn't parse, "3" for a child that
        isn't allowed or is repeated."""
        found = {}
        pos = 0
        for m in _CHILD.finditer(inner):
            if m.start() != pos:
                return "2"
            child, value = m.groups()
            if child not in allowed or child in found:
                return "3"
            found[child] = value
            pos = m.end()
        if inner[pos:].strip():
            return "2"
        return found

    @staticmethod
    def _in(value, lo, hi):
        return bool(re.fullmatch(r"-?\d{1,10}", value)) and lo <= int(value) <= hi

    @staticmethod
    def _length(value, hi, lo=0):
        plain = value.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
        return lo <= len(plain) <= hi

    def _write(self, tag, inner):
        """Carry out one write; the answer code."""
        if tag in PRINTER_WIDE_WRITES:
            return "0"          # and nothing changes
        if tag in ("CTEXT", "CTDAT", "CLOT", "CCONT"):
            answer = self._add_item(tag, inner)
        elif tag not in ALLOWED_WRITE_TAGS:
            return "1"
        elif tag == "CPPARM":
            answer = self._cpparm(inner)
        elif tag == "TD":
            answer = self._td(inner)
        else:
            if "<" in inner:
                return "2"
            answer = {"CRTMSG": self._crtmsg, "SAVEMSG": self._savemsg, "PRINTMSG": self._printmsg,
                      "PRINT": self._print}[tag](inner)
        if answer == "0" and self.write_echo and tag in ("PRINT", "PRINTMSG", "SAVEMSG"):
            return f"0 {inner}"
        return answer

    def _crtmsg(self, name):
        if not self._length(name, 20):
            return "3"
        self._design = {"name": name, "items": [], "settings": dict(FAKE_DEFAULT_SETTINGS)}
        self._next_objid = 1
        return "0"

    def _add_item(self, tag, inner):
        fonts = len(self.fonts)
        common = {"OBJID": lambda v: self._in(v, 10000, 20000), "POS": self._point, "MARG": self._point,
                  "FONTIND": lambda v: self._in(v, 1, fonts), "SIZE": lambda v: self._in(v, 1, 10),
                  "PREF": lambda v: self._length(v, 100), "POSF": lambda v: self._length(v, 100),
                  "XRID": lambda v: self._in(v, 10000, 20000), "YRID": lambda v: self._in(v, 10000, 20000),
                  "SXAS": lambda v: self._in(v, 0, 1), "SYAS": lambda v: self._in(v, 0, 1)}
        specific = {
            "CTEXT": {"TXT": lambda v: self._length(v, 250), "LANG": lambda v: self._in(v, 0, 65535)},
            "CTDAT": {"FRMT": lambda v: self._length(v, 100, 1), "DAY": lambda v: self._in(v, 0, 999),
                      "MON": lambda v: self._in(v, 0, 999), "YEAR": lambda v: self._in(v, 0, 99)},
            "CLOT": {"INDX": lambda v: self._in(v, 1, 9)},
            "CCONT": {"SRC": lambda v: self._in(v, 0, 3), "STRVAL": lambda v: self._in(v, 0, 1_000_000),
                      "ENDVAL": lambda v: self._in(v, 0, 1_000_000), "STEP": lambda v: self._in(v, 0, 1_000_000),
                      "PULS": lambda v: self._in(v, 0, 100)},
        }[tag]
        checks = dict(common, **specific)
        if self._design is None:
            return "5"
        found = self._children(inner, checks)
        if isinstance(found, str):
            return found
        if not all(checks[k](v) for k, v in found.items()):
            return "3"
        if "OBJID" in found:
            objid = found["OBJID"]
        else:
            objid = str(self._next_objid)
            self._next_objid += 1
        get = found.get
        if tag == "CTDAT":
            first = [("FRMT", get("FRMT", "")), ("AMPM", "\xff"), ("DAY", get("DAY", "0")), ("DWEEK", "0"),
                     ("HOUR", "0"), ("HORFLG", "1"), ("LANG", "17742"), ("MIN", "0"), ("MON", get("MON", "0")),
                     ("SEC", "0"), ("YEAR", get("YEAR", "0"))]
        elif tag == "CLOT":
            first = [("INDX", get("INDX", "1"))]
        elif tag == "CCONT":
            first = [("SRC", get("SRC", "0")), ("STRVAL", get("STRVAL", "1")), ("ENDVAL", get("ENDVAL", "1000")),
                     ("STEP", get("STEP", "1")), ("PULS", get("PULS", "0"))]
        else:
            first = []
        pairs = first + [
            ("LANG", "4"), ("TXT", get("TXT", "") if tag == "CTEXT" else ""), ("FONTIND", get("FONTIND", "1")),
            ("SIZE", str(int(get("SIZE", "1")) - 1)), ("POS", get("POS", "0,0")), ("BOUND", ""),
            ("POSF", get("POSF", "")), ("PREF", get("PREF", "")), ("ROTAT", "0"), ("WCOEFF", "0"), ("GROUP", "0"),
            ("OBJTYP", "0" if tag == "CTEXT" else "1"), ("COLR", "0"), ("XRID", get("XRID", "0")),
            ("YRID", get("YRID", "0")), ("SXAS", get("SXAS", "0")), ("SYAS", get("SYAS", "0")), ("OBJID", objid),
            ("RP", "0"), ("MARG", get("MARG", "0,0"))]
        self._design["items"].append(DesignItem(tag=tag, pairs=pairs))
        return "0"

    @staticmethod
    def _point(value):
        return bool(re.fullmatch(r"-?\d{1,5},-?\d{1,5}", value))

    def _cpparm(self, inner):
        if self._design is None:
            return "5"
        us = lambda v: self._in(v, 0, 120_000_000)
        flag = lambda v: self._in(v, 0, 1)
        checks = {"DELAY": us, "PRNSPD": lambda v: self._in(v, 1, 2500), "ENCSCL": lambda v: self._in(v, 0, 100),
                  "DLYMM": us, "BOLD": lambda v: self._in(v, 1, 10), "WDTH": lambda v: self._in(v, 1, 10),
                  "QUANT": lambda v: self._in(v, 1, 100), "QDELAY": us, "QDLYMM": us, "ENCST": flag,
                  "RESMOD": lambda v: self._in(v, 0, 3), "ROTAT": flag, "MIRR": flag, "REVRS": flag,
                  "AUTREP": flag}
        found = self._children(inner, checks)
        if isinstance(found, str):
            return found
        if not all(checks[k](v) for k, v in found.items()):
            return "3"
        self._design["settings"].update(found)
        if "RESMOD" in found:
            self._design["settings"]["RESNAM"] = RESOLUTION_NAMES[int(found["RESMOD"])]
        return "0"

    def _savemsg(self, value):
        if not self._in(value, 1, 10):
            return "3"
        if self._design is None:
            return "5"
        n = int(value)
        d = self._design
        self.slots[n] = MessageDesign(slot=n, name=d["name"], empty=False, width=256, height=160,
                                      items=d["items"], settings=d["settings"])
        self.slot_style[n] = "verbose"
        self._design = None
        self.buffer = self._save_result(n)
        return "0"

    def _printmsg(self, value):
        if not self._in(value, 1, 10):
            return "3"
        n = int(value)
        if n not in self.slots:
            return "4"      # an empty slot (OPEN)
        if n != self.current_slot:
            self.counters["MSCNT"] = 0      # OPEN: does it reset on a message change?
        self.current_slot = n
        return "0"

    def _print(self, value):
        if value not in ("0", "1"):
            return "3"
        self.printing = value == "1"
        return "0"

    def _td(self, inner):
        ranges = {"DAY": (1, 31), "MNTH": (1, 12), "YEAR": (2000, 2050), "HOUR": (0, 24), "MIN": (0, 59),
                  "DTRH": (0, 24), "DTRM": (0, 59)}
        found = self._children(inner, ranges)
        if isinstance(found, str):
            return found
        if not all(self._in(v, *ranges[k]) for k, v in found.items()):
            return "3"
        now = self.printer_clock()
        get = lambda key, default: int(found[key]) if key in found else default
        hour = get("HOUR", now.hour) % 24
        try:
            new = datetime(get("YEAR", now.year), get("MNTH", now.month), get("DAY", now.day), hour,
                           get("MIN", now.minute))
        except ValueError:
            return "3"      # not a real date
        self.clock_offset_s = (new - _EPOCH).total_seconds() - (self.clock.time() + self.base_offset_s)
        if "DTRH" in found or "DTRM" in found:
            old = self.rollover or (0, 0)
            self.rollover = (get("DTRH", old[0]), get("DTRM", old[1]))
        return "0"


# --------------------------------------------------------------------------
# FakeSntp
# --------------------------------------------------------------------------

class FakeSntp:
    """An SNTP server on UDP 127.0.0.1. It stands for another machine, so it uses real time."""

    def __init__(self, *, offset_s=0.0, silent=False):
        self.offset_s = offset_s
        self.silent = silent
        self.requests = 0
        self.port = 0
        self._sock = None
        self._thread = None
        self._stopping = threading.Event()

    def start(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.settimeout(0.05)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._run, name=f"hx-fake-sntp-{self.port}", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        if self._sock is None:
            return
        self._stopping.set()
        self._thread.join(timeout=2.0)
        self._sock.close()
        self._sock = None

    def _run(self):
        while not self._stopping.is_set():
            try:
                data, addr = self._sock.recvfrom(1024)
            except socket.timeout:
                continue
            except ConnectionResetError:
                continue        # Windows reports an earlier reply that couldn't be delivered
            except OSError:
                return
            self.requests += 1
            if self.silent or len(data) < 48:
                continue
            try:
                self._sock.sendto(self._answer(data), addr)
            except OSError:
                pass

    def _answer(self, request):
        ntp = time.time() + self.offset_s + 2_208_988_800
        seconds = int(ntp)
        stamp = struct.pack("!II", seconds & 0xFFFFFFFF, int((ntp - seconds) * 2 ** 32) & 0xFFFFFFFF)
        version = (request[0] >> 3) & 7
        # LI 0, the request's version, mode 4 (server); stratum 2; its poll; precision -20
        head = bytes([(version << 3) | 4, 2, request[2], (-20) & 0xFF]) + bytes(8) + b"FAKE"
        return head + stamp + request[40:48] + stamp + stamp


# --------------------------------------------------------------------------
# FakeWorld
# --------------------------------------------------------------------------

# FakePrinter knobs a FakeWorld printer dict may set besides the constructor's keywords
_KNOBS = ("hx_name", "mixup_p", "drop_after_idle_p", "overload_at", "overload_pause_s", "multi_value_hang_s",
          "web_latency_s", "web_result_delay_s", "web_first_reply_fresh", "web_reply_hook",
          "single_web_session", "write_echo", "gzip_files", "files")
_CONSTRUCTOR = ("uid", "password", "placement", "fonts", "firmware", "latency_s", "mixup", "busy_on_overlap",
                "drop_after_idle_s", "editor_page_bytes", "editor_stall_s", "seed", "offset_fn")
# The share folders the folder script creates (storage.md §1.1)
_FOLDERS = ("config/hx/lines", "config/hx/machines", "config/hx/printers", "config/hx/adjustments",
            "config/hx/calibrations", "templates/products", "templates/films", "templates/print-texts",
            "status/deployment", "status/machines", "status/lines", "status/locks", "status/alerts",
            "status/pollers", "logs/hx-sends", "logs/hx-events", "logs/hx-admin")


class FakeWorld:
    """A temp share, fake printers and a fake time server, seeded with example records (Example Co,
    Line 1 Bagger 1, ...) with hx_share.write_record plus a changes.json (protocol.md §16.2)."""

    def __init__(self, tmpdir, *, printers=(), clock=SYSTEM_CLOCK, timings=TIMINGS):
        self.tmpdir = tmpdir
        self.clock = clock
        self.timings = timings
        self.share_root = os.path.join(tmpdir, "share", "v2")
        # A test world has no folder script, so it makes the script's folders itself
        for folder in _FOLDERS:
            os.makedirs(os.path.join(self.share_root, *folder.split("/")), exist_ok=True)
        # The share's not-found cache and listing delay run on the world's clock, like everything else
        self.fs = hx_share.FlakyFileOps(clock=clock)
        self.printers = []
        self.sntp = None
        self.ids = {}
        try:
            names = []
            for n, spec in enumerate(list(printers) or [{}], start=1):
                spec = dict(spec)
                names.append(spec.pop("name", f"Printer {n}"))
                self.printers.append(self._printer(spec).start())
            self.sntp = FakeSntp().start()
            self._seed(names)
        except BaseException:
            self.close()
            raise

    def _printer(self, spec):
        unknown = [k for k in spec if k not in _CONSTRUCTOR and k not in _KNOBS]
        if unknown:
            raise TypeError(f"FakeWorld printer: unknown setting {unknown[0]!r}")
        fake = FakePrinter(clock=self.clock, **{k: v for k, v in spec.items() if k in _CONSTRUCTOR})
        for k, v in spec.items():
            if k in _KNOBS:
                setattr(fake, k, v)
        return fake

    def _seed(self, names):
        store = self.store(self.identity("admin", admin=True, sender=False))
        now = self.clock.time()
        base = {"revision": 1, "updated_by": "EXAMPLE\\admin", "updated_at": now}
        write = lambda record: hx_share.write_record(store, record)
        write(Company(id="company", **base, time_server=f"127.0.0.1:{self.sntp.port}",
                      countries=[Country(name="USA", aliases=["US", "U.S.A.", "United States"]),
                                 Country(name="Canada", aliases=["CAN"]), Country(name="Mexico", aliases=["MEX"])],
                      inks=[Ink(name="Example Black", cartridge="plastic", colour="black"),
                            Ink(name="Example White", cartridge="plastic", colour="white"),
                            Ink(name="Example Paper Black", cartridge="paper", colour="black")],
                      admin_group_sid=ADMIN_SID, sender_group_sid=SENDER_SID))
        self.ids["company"] = "company"
        line = Line(id=new_id("ln"), **base, name="Line 1", code="L1", cartridge="plastic",
                    notify_user="EXAMPLE\\jsmith")
        write(line)
        self.ids["line"] = line.id
        for n, (fake, name) in enumerate(zip(self.printers, names), start=1):
            printer = fake.printer_record(name=name, **base)
            write(printer)
            encoder = fake.placement == "encoder"
            machine = Machine(id=new_id("mc"), **base, name=f"Line 1 Bagger {n}", code=f"B{n}", line_id=line.id,
                              printer_id=printer.id, placement=fake.placement, print_speed=2400,
                              head_distance_mm=412.0 if encoder else None,
                              encoder_wheel_mm=56.0 if encoder else None, encoder_ppr=5000 if encoder else None)
            write(machine)
            self.ids[f"printer{n}"] = printer.id
            self.ids[f"machine{n}"] = machine.id
        product = Product(id=new_id("pd"), **base, name="Example Co 2lb Onion", customer="Example Co")
        write(product)
        self.ids["product"] = product.id
        print_text = PrintText(id=new_id("pt"), **base, name="Packed on + origin", code="PKO", status="active",
                               items=[PrintTextItem(id="i1", kind="packed_date", date_format="MM/DD/YY",
                                                    before="Packed on "),
                                      PrintTextItem(id="i2", kind="origin", placement="below"),
                                      PrintTextItem(id="i3", kind="lot", before="Lot ", placement="right")])
        write(print_text)
        self.ids["print_text"] = print_text.id
        film = Film(id=new_id("fm"), **base, product_id=product.id, code="EXCO1", artwork_revision="Rev C",
                    status="active", side="back", cartridge="plastic", ink_colour="black", length_mm=610.0,
                    position_mm=150.0, delay_ms=480.0, font=2, size=4, resolution=0, bold=1, width=1,
                    print_text_ids=[print_text.id], default_print_text_id=print_text.id)
        os.makedirs(os.path.join(self.share_root, "templates", "films", film.id), exist_ok=True)
        write(film)
        self.ids["film"] = film.id
        store.write_json("config/hx/changes.json", {"file_type": "changes", "counter": 1, "written_at": now,
                                                    "written_by": "EXAMPLE\\admin", "last": None})

    def identity(self, name, *, admin=False, sender=True, pid=1):
        sids = ([SENDER_SID] if sender else []) + ([ADMIN_SID] if admin else [])
        return Identity(user=f"EXAMPLE\\{name}", computer=f"PC-{name.upper()}", sids=frozenset(sids), pid=pid)

    def store(self, identity):
        """Each simulated PC gets its own store, on its own fs.view() (its own caches and calls)."""
        return hx_share.ShareStore(self.share_root, identity=identity, clock=self.clock, fs=self.fs.view(),
                                   timings=self.timings, version="test")

    def paths(self, identity):
        pc = os.path.join(self.tmpdir, "pcs", identity.key)
        machine = os.path.join(self.tmpdir, "pcs", re.sub(r"[^A-Za-z0-9._-]+", "_", identity.computer), "machine")
        paths = hx_share.HxPaths(share_root=self.share_root, roaming=os.path.join(pc, "roaming"),
                                 local=os.path.join(pc, "local"), machine=machine)
        for folder in (paths.roaming, paths.local, paths.machine):
            os.makedirs(folder, exist_ok=True)
        return paths

    def time_source(self):
        """A TimeSource asking the fake time server, the same host the seeded company names."""
        return hx_time.TimeSource([f"127.0.0.1:{self.sntp.port}"], clock=self.clock)

    def close(self):
        """Stop every fake. The test owns tmpdir."""
        for fake in self.printers:
            fake.stop()
        if self.sntp is not None:
            self.sntp.stop()


# --------------------------------------------------------------------------
# The script
# --------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description="Fake MapleJet printers and a seeded temp share for Stripes.")
    parser.add_argument("--printers", type=int, default=1, help="how many fake printers (default 1)")
    parser.add_argument("--encoder", action="store_true", help="encoder machines instead of delay machines")
    parser.add_argument("--mixup", action="store_true", help="mix up replies to overlapping requests")
    args = parser.parse_args(argv)
    tmp = tempfile.mkdtemp(prefix="stripes-hx-fake-")
    spec = {"placement": "encoder" if args.encoder else "delay", "mixup": args.mixup, "latency_s": 0.08,
            "web_latency_s": 0.8}       # about as slow as the real printer
    world = FakeWorld(tmp, printers=[dict(spec) for _ in range(max(1, args.printers))])
    try:
        paths = world.paths(world.identity("jsmith", admin=True, sender=True))
        values = (("STRIPES_V2", world.share_root), ("STRIPES_V2_ROAMING", paths.roaming),
                  ("STRIPES_V2_LOCAL", paths.local), ("STRIPES_V2_MACHINE", paths.machine),
                  ("STRIPES_HX_USER", "EXAMPLE\\jsmith"), ("STRIPES_HX_COMPUTER", "PC-JSMITH"),
                  ("STRIPES_HX_SIDS", f"{SENDER_SID},{ADMIN_SID}"))
        for name, value in values:
            if sys.platform == "win32":
                print(f"set {name}={value}")
            else:
                print(f"export {name}={shlex.quote(value)}")
        ports = ", ".join(str(p.port) for p in world.printers)
        print(f"# {len(world.printers)} {spec['placement']} fake printer(s) on 127.0.0.1 port {ports}; "
              f"time server on UDP port {world.sntp.port}. Ctrl+C stops.", flush=True)
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        world.close()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
