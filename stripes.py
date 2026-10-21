"""Stripes - type some text, pick a printer, print labels on Zebra printers.

Sends raw ZPL to Zebra printers, either through a printer installed in
Windows (via the print spooler) or straight to an IP address on port 9100.
Needs pywin32 for Windows printers; everything else is standard library.
"""

import gc
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import queue
import urllib.request
from dataclasses import dataclass, asdict, field, fields, replace
from datetime import datetime
from math import inf

import tkinter as tk
import tkinter.font
from tkinter import ttk, messagebox

APP_NAME = "Stripes"
try:
    from stripes_version import VERSION  # written by the release build
except ImportError:
    VERSION = "dev"

# Dots per millimetre for each print resolution (what ZPL and Labelary use).
DPI_TO_DPMM = {203: 8, 300: 12, 600: 24}

# Default label size (width, height in inches) for each printer model.
MODELS = {
    "ZD420": (2.0, 1.0),
    "ZT410": (3.0, 3.0),
    "Other": (4.0, 6.0),
}

MARGIN_IN = 0.06       # blank border around the text
LINE_GAP = 0.18        # space between lines, as a fraction of text height
EMPHASIS = 1.5         # size of the first line when "Bigger first line" is on
WIDTH_SAFETY = 1.08    # fudge factor on the character width estimate
MAX_RECENT = 20
MAX_COPIES = 9999
CONFIRM_COPIES_OVER = 50


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------

@dataclass
class Printer:
    name: str
    host: str = ""          # IP address; optional when windows_name is set
    port: int = 9100
    windows_name: str = ""  # printer installed in Windows; used for printing when set
    model: str = "ZD420"
    width_in: float = 2.0
    height_in: float = 1.0
    dpi: int = 203
    shift_x: int = 0   # nudge everything right (+) or left (-), in dots
    shift_y: int = 0   # nudge everything down (+) or up (-), in dots
    stock: str = ""    # id of the printer's default label stock (company printers)
    expected: dict = field(default_factory=dict)  # how the printer should be set up; checked, never sent

    @property
    def dpmm(self):
        return DPI_TO_DPMM[self.dpi]

    @property
    def where(self):
        return f"Windows printer '{self.windows_name}'" if self.windows_name else f"IP {self.host}"

    @property
    def size_text(self):
        return f'{self.width_in:g}" wide x {self.height_in:g}" tall, {self.dpi} dpi'

    @classmethod
    def from_dict(cls, d):
        return _from_dict(cls, d)


def _from_dict(cls, d):
    """Build a dataclass from a JSON object, ignoring keys it doesn't know."""
    if not isinstance(d, dict):
        raise TypeError("each entry must be a {...} object")
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in d.items() if k in known})


MEDIA_TYPES = ("gap", "mark", "continuous")


@dataclass
class Stock:
    """A kind of label roll."""
    id: str
    name: str
    width_in: float
    height_in: float
    media: str = "gap"  # gap (gap/notch), mark (black mark) or continuous

    @classmethod
    def from_dict(cls, d):
        return _from_dict(cls, d)


@dataclass
class CompanyConfig:
    """Printers and label stock that IT manages for every PC."""
    printers: list = field(default_factory=list)
    stock: list = field(default_factory=list)
    source: str = None     # where it was read from
    problems: list = field(default_factory=list)
    cached_at: str = None  # set when the server was unreachable and the saved copy is in use


def settings_path():
    override = os.environ.get("STRIPES_SETTINGS")
    if override:
        return override
    base = os.environ.get("APPDATA") or os.path.expanduser("~/.config")
    return os.path.join(base, "Stripes", "settings.json")


def load_settings():
    data = {}
    try:
        with open(settings_path(), encoding="utf-8-sig") as f:
            data = json.load(f)
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:
        print(f"Could not read settings: {e}", file=sys.stderr)
    printers = []
    for p in data.get("printers", []):
        try:
            printers.append(Printer.from_dict(p))
        except TypeError:
            pass
    return {
        "printers": printers,
        "last_printer": data.get("last_printer"),
        "recent": data.get("recent", []),
    }


def configured_folder():
    r"""The company config folder that Group Policy points this PC at, if any.

    Set by IT as HKLM\SOFTWARE\Policies\Stripes\ConfigPath (e.g. a \\server\share path),
    so the location never needs to be written into the app.
    """
    override = os.environ.get("STRIPES_CONFIG")
    if override:
        return override
    if sys.platform != "win32":
        return None
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Policies\Stripes") as key:
            return str(winreg.QueryValueEx(key, "ConfigPath")[0]).strip() or None
    except OSError:
        return None


def local_folders():
    """Fallback config folders on the PC itself, for PCs without the registry setting."""
    folders = []
    if os.environ.get("PROGRAMDATA"):
        folders.append(os.path.join(os.environ["PROGRAMDATA"], "Stripes"))
    here = sys.executable if getattr(sys, "frozen", False) else __file__
    folders.append(os.path.dirname(os.path.abspath(here)))
    return folders


def cache_folder():
    override = os.environ.get("STRIPES_CACHE")
    if override:
        return override
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.cache")
    return os.path.join(base, "Stripes", "config-cache")


def _read_json(path):
    with open(path, encoding="utf-8-sig") as f:  # tolerate Notepad's BOM
        return json.load(f)


def _entries(data, key):
    return data.get(key, []) if isinstance(data, dict) else []


def parse_config(folder):
    """Read printers.json and (optional) stock.json from a folder.

    Raises OSError/ValueError if printers.json itself can't be read; bad entries
    are skipped and listed in the returned problems.
    """
    problems = []
    try:
        stock_data = _read_json(os.path.join(folder, "stock.json"))
    except FileNotFoundError:
        stock_data = {}
    except (OSError, ValueError) as e:
        stock_data = {}
        problems.append(f"Could not read stock.json: {e}")
    stock, by_id = [], {}
    for i, d in enumerate(_entries(stock_data, "stock")):
        try:
            st = Stock.from_dict(d)
            st.width_in, st.height_in = float(st.width_in), float(st.height_in)
            if st.width_in <= 0 or st.height_in <= 0:
                raise ValueError("width_in and height_in must be above 0")
            if st.media not in MEDIA_TYPES:
                raise ValueError(f"media must be one of {', '.join(MEDIA_TYPES)}")
            if st.id in by_id:
                raise ValueError("duplicate id")
        except (TypeError, ValueError) as e:
            problems.append(f"stock.json entry {i + 1} skipped: {e}")
            continue
        by_id[st.id] = st
        stock.append(st)

    printer_data = _read_json(os.path.join(folder, "printers.json"))
    printers, names = [], set()
    for i, d in enumerate(_entries(printer_data, "printers")):
        try:
            p = Printer.from_dict(d)
            if p.dpi not in DPI_TO_DPMM:
                raise ValueError(f"dpi must be one of {', '.join(map(str, DPI_TO_DPMM))}")
            if not p.host and not p.windows_name:
                raise ValueError("needs a host (IP address) or windows_name")
            if p.name in names:
                raise ValueError("duplicate name")
            if not isinstance(p.expected, dict):
                raise ValueError("expected must be a {...} object")
        except (TypeError, ValueError) as e:
            problems.append(f"printers.json entry {i + 1} skipped: {e}")
            continue
        if p.stock:
            if p.stock in by_id:
                p.width_in, p.height_in = by_id[p.stock].width_in, by_id[p.stock].height_in
            else:
                problems.append(f"Printer {p.name}: unknown stock '{p.stock}'")
                p.stock = ""
        names.add(p.name)
        printers.append(p)
    return printers, stock, problems


def save_cache(folder):
    """Keep a copy of the server's config so the app works when the server is down."""
    cache = cache_folder()
    os.makedirs(cache, exist_ok=True)
    for name in ("printers.json", "stock.json"):
        src, dst = os.path.join(folder, name), os.path.join(cache, name)
        if os.path.exists(src):
            shutil.copyfile(src, dst + ".tmp")
            os.replace(dst + ".tmp", dst)
        elif os.path.exists(dst):
            os.remove(dst)


def load_cached_config(folder):
    """The saved copy of the server's config, or None if there isn't one."""
    cache = cache_folder()
    try:
        printers, stock, problems = parse_config(cache)
        stamp = os.path.getmtime(os.path.join(cache, "printers.json"))
    except (OSError, ValueError):
        return None
    return CompanyConfig(printers, stock, folder, problems,
                         cached_at=datetime.fromtimestamp(stamp).strftime("%b %d %Y, %I:%M %p"))


def load_company_config():
    """Read the company config: from the server if one is configured, else a local folder.

    Can be slow when the server is unreachable, so the app calls this off the UI thread.
    """
    folder = configured_folder()
    if folder:
        try:
            printers, stock, problems = parse_config(folder)
        except (OSError, ValueError) as e:
            cached = load_cached_config(folder)
            if cached:
                cached.problems.insert(0, f"Couldn't read the company settings from {folder}; "
                                          f"using the copy saved {cached.cached_at}.")
                return cached
            return CompanyConfig(source=folder, problems=[f"Couldn't read the company settings from {folder}: {e}"])
        try:
            save_cache(folder)
        except OSError:
            pass  # the app still works, just without an offline copy
        return CompanyConfig(printers, stock, folder, problems)
    for local in local_folders():
        if os.path.exists(os.path.join(local, "printers.json")):
            try:
                printers, stock, problems = parse_config(local)
            except (OSError, ValueError) as e:
                path = os.path.join(local, "printers.json")
                return CompanyConfig(source=local, problems=[f"Could not read {path}: {e}"])
            return CompanyConfig(printers, stock, local, problems)
    return CompanyConfig()


def merge_printers(company, personal):
    """Company printers first; a personal printer with the same name is hidden."""
    names = {p.name for p in company}
    return list(company) + [p for p in personal if p.name not in names]


def save_settings(settings):
    path = settings_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = dict(settings, printers=[asdict(p) for p in settings["printers"]])
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# Layout and ZPL
# --------------------------------------------------------------------------

# Rough character widths for Zebra font 0 (CG Triumvirate Bold Condensed),
# as a fraction of the character height. Used to keep lines inside the label.
_NARROW = set("il.,:;'!|`")
_SEMI = set("fjrtI-()[]/\\\" ")
_WIDE_LOWER = set("mw")
_WIDE_UPPER = set("MW@%")


def char_em(c):
    if c in _NARROW:
        return 0.25
    if c in _SEMI:
        return 0.33
    if c in _WIDE_UPPER:
        return 0.82
    if c in _WIDE_LOWER:
        return 0.75
    if c.isdigit():
        return 0.52
    if c.islower():
        return 0.50
    if c.isupper():
        return 0.60
    return 0.60


def text_em(s):
    return sum(char_em(c) for c in s)


@dataclass
class PlacedLine:
    text: str
    y: int   # top of the line, in dots
    h: int   # character height, in dots


@dataclass
class Layout:
    width: int        # label width, dots
    height: int       # label height, dots
    margin: int       # dots
    lines: list
    warnings: list

    @property
    def inner_width(self):
        return self.width - 2 * self.margin


def label_dots(printer):
    dots_per_inch = printer.dpmm * 25.4
    return round(printer.width_in * dots_per_inch), round(printer.height_in * dots_per_inch)


def layout_label(text, printer, size_pt=None, emphasize_first=False):
    """Work out the size and position of each line.

    size_pt=None means auto: make the text as large as fits.
    """
    dots_per_inch = printer.dpmm * 25.4
    width, height = label_dots(printer)
    margin = round(MARGIN_IN * dots_per_inch)
    avail_w = width - 2 * margin
    avail_h = height - 2 * margin

    lines = [ln.rstrip() for ln in text.splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return Layout(width, height, margin, [], [])

    n = len(lines)
    weights = [EMPHASIS if (emphasize_first and i == 0 and n > 1) else 1.0 for i in range(n)]

    def total(hs):
        return sum(hs) + LINE_GAP * sum(hs[:-1])

    def width_cap(line):
        em = text_em(line) * WIDTH_SAFETY
        return avail_w / em if em else inf

    if size_pt is None:
        unit = avail_h / total(weights)
        body = min([unit] + [width_cap(ln) for ln, w in zip(lines, weights) if w == 1.0])
        hs = [min(unit * w, width_cap(ln)) if w != 1.0 else body for ln, w in zip(lines, weights)]
    else:
        body = size_pt / 72 * dots_per_inch
        hs = [body * w for w in weights]
    hs = [max(1, int(h)) for h in hs]

    warnings = []
    if total(hs) > avail_h + 0.5:
        warnings.append("Text is too tall for the label. Use fewer lines or a smaller size.")
    for i, (ln, h) in enumerate(zip(lines, hs)):
        if text_em(ln) * WIDTH_SAFETY * h > avail_w + 0.5:
            warnings.append(f"Line {i + 1} is probably too wide for the label.")
    if min(hs) < 0.06 * dots_per_inch:
        warnings.append("Text is very small and may be hard to read.")

    y = margin + max(0.0, (avail_h - total(hs)) / 2)
    placed = []
    for ln, h in zip(lines, hs):
        placed.append(PlacedLine(ln, round(y), h))
        y += h * (1 + LINE_GAP)
    return Layout(width, height, margin, placed, warnings)


def zpl_escape(s):
    # ^FH turns _XX into the byte 0xXX, which lets us print ^ ~ and _ safely.
    s = "".join(c for c in s if c >= " ")
    return s.replace("_", "_5F").replace("^", "_5E").replace("~", "_7E")


def build_zpl(layout, printer, align="center", copies=1, border=False):
    """The label format. It only places text: no printer settings (width, length,
    media, darkness...) are sent, because those stay in effect on the printer and
    would change how other software's labels print."""
    just = {"left": "L", "center": "C", "right": "R"}[align]
    sx, sy = printer.shift_x, printer.shift_y
    out = ["^XA"]
    if any(ord(c) > 126 for ln in layout.lines for c in ln.text):
        out.append("^CI28")  # UTF-8, only when needed: it also stays in effect
    if border:
        out.append(f"^FO{max(0, sx)},{max(0, sy)}^GB{layout.width},{layout.height},3^FS")
    for ln in layout.lines:
        if not ln.text.strip():
            continue
        x = max(0, layout.margin + sx)
        y = max(0, ln.y + sy)
        out.append(
            f"^FO{x},{y}^A0N,{ln.h},{ln.h}"
            f"^FB{layout.inner_width},1,0,{just},0^FH^FD{zpl_escape(ln.text)}^FS"
        )
    out.append(f"^PQ{int(copies)}")
    out.append("^XZ")
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------
# Talking to printers
# --------------------------------------------------------------------------

def send_raw(host, port, data, timeout=5):
    with socket.create_connection((host, port), timeout=timeout) as s:
        s.sendall(data)
        s.shutdown(socket.SHUT_WR)


def parse_host_status(reply):
    """Turn a ~HS reply into a list of problems (empty list = ready)."""
    parts = re.findall(rb"\x02(.*?)\x03", reply, re.S)
    if len(parts) < 2:
        raise ValueError("The printer sent an unexpected status reply.")
    s1 = parts[0].decode("ascii", "replace").split(",")
    s2 = parts[1].decode("ascii", "replace").split(",")

    def flag(fields_, i):
        return len(fields_) > i and fields_[i].strip() == "1"

    problems = []
    if flag(s1, 1):
        problems.append("Out of labels")
    if flag(s1, 2):
        problems.append("Paused")
    if flag(s2, 2):
        problems.append("Print head open")
    if flag(s2, 3):
        problems.append("Ribbon out")
    if flag(s1, 10):
        problems.append("Print head too cold")
    if flag(s1, 11):
        problems.append("Print head too hot")
    return problems


def query_status(host, port, timeout=3):
    reply = b""
    with socket.create_connection((host, port), timeout=timeout) as s:
        s.sendall(b"~HS")
        while reply.count(b"\x03") < 3:
            chunk = s.recv(1024)
            if not chunk:
                break
            reply += chunk
    return parse_host_status(reply)


# Lines of the printer's configuration report (^HH) that we compare with the spec.
_REPORT_FIELDS = {
    "DARKNESS": "darkness",
    "PRINT SPEED": "speed",
    "PRINT MODE": "print_mode",
    "MEDIA TYPE": "media",
    "PRINT WIDTH": "print_width",
}
_REPORT_MEDIA = {"GAP/NOTCH": "gap", "NON-CONTINUOUS": "gap", "WEB": "gap", "MARK": "mark",
                 "BLACK MARK": "mark", "CONTINUOUS": "continuous"}


def parse_config_report(text):
    """Pull the settings we care about out of a ^HH configuration report.

    Report lines look like "  +10.0               DARKNESS" (value, then name).
    Anything unrecognised is skipped, so an unexpected format just means fewer checks.
    """
    found = {}
    for line in text.splitlines():
        m = re.match(r"^\s*(\S.*?)\s{2,}([A-Z][A-Z0-9 ./-]*?)\s*$", line)
        if not m or m.group(2) not in _REPORT_FIELDS:
            continue
        key, value = _REPORT_FIELDS[m.group(2)], m.group(1).strip().upper()
        number = re.match(r"[+-]?\d+(\.\d+)?", value)
        if key in ("darkness", "speed") and number:
            found[key] = float(number.group())
        elif key == "print_width" and number:
            found[key] = int(float(number.group()))
        elif key == "print_mode":
            found[key] = value.lower().replace(" ", "-")  # TEAR OFF -> tear-off
        elif key == "media":
            found[key] = _REPORT_MEDIA.get(value, value.lower())
    return found


def query_config(host, port, timeout=4):
    """Ask the printer for its configuration report. Read-only."""
    reply = b""
    with socket.create_connection((host, port), timeout=timeout) as s:
        s.sendall(b"^XA^HH^XZ")
        try:
            while b"\x03" not in reply:
                chunk = s.recv(4096)
                if not chunk:
                    break
                reply += chunk
        except socket.timeout:
            pass
    return parse_config_report(reply.decode("ascii", "replace"))


_SPEC_LABELS = {"darkness": "Darkness", "speed": "Speed", "print_mode": "Print mode",
                "media": "Media type", "print_width": "Print width"}


def spec_differences(expected, actual):
    """Human-readable differences between a printer's spec and what it reports."""
    diffs = []
    for key, label in _SPEC_LABELS.items():
        if key not in expected or key not in actual:
            continue
        want, have = expected[key], actual[key]
        if isinstance(have, (int, float)):
            try:
                same = abs(float(want) - have) < 0.05
            except (TypeError, ValueError):
                same = False
        else:
            same = str(want).strip().lower().replace(" ", "-") == have
        if not same:
            diffs.append(f"{label} is {have:g}" if isinstance(have, float) else f"{label} is {have}")
            diffs[-1] += f" (spec says {want})"
    return diffs


def check_printer(printer):
    """Status problems, plus spec differences for company printers with a spec.

    Returns (problems, differences); differences is None when not checked.
    """
    problems = printer_problems(printer)
    if not (printer.expected and printer.host):
        return problems, None
    try:
        return problems, spec_differences(printer.expected, query_config(printer.host, printer.port))
    except OSError:
        return problems, None


def describe_error(exc):
    if isinstance(exc, socket.timeout):
        return "No answer from the printer (timed out). Is it on and connected?"
    if isinstance(exc, ConnectionRefusedError):
        return "The printer refused the connection. Check the IP address and port."
    if isinstance(exc, socket.gaierror):
        return "Unknown printer address. Check the IP address."
    if win32print is not None and exc.__class__.__name__ == "error" and len(getattr(exc, "args", ())) >= 3:
        return f"Windows printer error: {exc.args[2]}"  # pywintypes.error
    if isinstance(exc, OSError) and exc.strerror:
        return f"Network error: {exc.strerror}"
    return str(exc) or exc.__class__.__name__


# ----- printers installed in Windows -----

try:
    import win32print
except ImportError:
    win32print = None

_ZEBRA_MODEL = re.compile(r"\b(Z[DT]\d{3}\w*|G[KXC]\d{3}\w*|ZM\d{3}|105SL\w*|ZE\d{3})", re.I)
_DRIVER_DPI = re.compile(r"(203|300|600)\s*dpi", re.I)
_PORT_IP = re.compile(r"(\d{1,3}(?:\.\d{1,3}){3})")

# Spooler status bits (PRINTER_STATUS_*) worth telling the user about.
_SPOOLER_PROBLEMS = [
    (0x00000080, "Offline"),
    (0x00000010, "Out of labels"),
    (0x00000001, "Paused"),
    (0x00400000, "Door/head open"),
    (0x00000008, "Label jam"),
    (0x00000002, "Error"),
]


def _port_addresses():
    """Map Windows printer port name -> IP address, as far as we can tell."""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-PrinterPort | Select-Object Name,PrinterHostAddress | ConvertTo-Json"],
            capture_output=True, text=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
        ports = json.loads(out or "[]")
        if isinstance(ports, dict):
            ports = [ports]
        return {p["Name"]: p["PrinterHostAddress"] for p in ports if p.get("PrinterHostAddress")}
    except Exception:
        return {}


def guess_printer(windows_name, driver, port, addresses):
    """Build a Printer for an installed Windows printer, guessing what we can."""
    text = f"{windows_name} {driver}"
    found = _ZEBRA_MODEL.search(text)
    raw_model = found.group(1).upper() if found else ""
    model = next((m for m in MODELS if m != "Other" and raw_model.startswith(m[:4])), "Other")
    dpi = _DRIVER_DPI.search(text)
    ip = addresses.get(port) or (_PORT_IP.search(port).group(1) if _PORT_IP.search(port) else "")
    w, h = MODELS[model]
    return Printer(name=windows_name, host=ip, windows_name=windows_name, model=model,
                   width_in=w, height_in=h, dpi=int(dpi.group(1)) if dpi else 203)


def is_zebra(windows_name, driver):
    text = f"{windows_name} {driver}".lower()
    return "zebra" in text or "zdesigner" in text or bool(_ZEBRA_MODEL.search(text))


def list_windows_printers():
    """Return [(Printer guess, is_zebra)] for every printer installed in Windows."""
    if win32print is None:
        raise RuntimeError("Windows printer support is not available (pywin32 is not installed).")
    flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
    infos = win32print.EnumPrinters(flags, None, 2)
    addresses = _port_addresses()
    result = []
    for info in infos:
        name, driver, port = info["pPrinterName"], info["pDriverName"] or "", info["pPortName"] or ""
        result.append((guess_printer(name, driver, port, addresses), is_zebra(name, driver)))
    result.sort(key=lambda r: (not r[1], r[0].name.lower()))
    return result


def print_windows_raw(windows_name, data):
    if win32print is None:
        raise RuntimeError("Windows printer support is not available (pywin32 is not installed).")
    h = win32print.OpenPrinter(windows_name)
    try:
        win32print.StartDocPrinter(h, 1, ("Stripes label", None, "RAW"))
        try:
            win32print.StartPagePrinter(h)
            win32print.WritePrinter(h, data)
            win32print.EndPagePrinter(h)
        finally:
            win32print.EndDocPrinter(h)
    finally:
        win32print.ClosePrinter(h)


def windows_printer_problems(windows_name):
    if win32print is None:
        raise RuntimeError("Windows printer support is not available (pywin32 is not installed).")
    h = win32print.OpenPrinter(windows_name)
    try:
        status = win32print.GetPrinter(h, 2)["Status"]
    finally:
        win32print.ClosePrinter(h)
    return [text for bit, text in _SPOOLER_PROBLEMS if status & bit]


def send_label(printer, data):
    if printer.windows_name:
        print_windows_raw(printer.windows_name, data)
    else:
        send_raw(printer.host, printer.port, data)


def printer_problems(printer):
    """Ask the printer itself when we know its IP, otherwise ask Windows."""
    if printer.host:
        return query_status(printer.host, printer.port)
    return windows_printer_problems(printer.windows_name)


def labelary_png(zpl, printer):
    url = (
        f"https://api.labelary.com/v1/printers/{printer.dpmm}dpmm/labels/"
        f"{printer.width_in:g}x{printer.height_in:g}/0/"
    )
    req = urllib.request.Request(
        url,
        data=zpl.encode("utf-8"),
        headers={"Accept": "image/png", "Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read()


# --------------------------------------------------------------------------
# User interface
# --------------------------------------------------------------------------

class Background:
    """Run slow work (network) off the UI thread and report back on it."""

    def __init__(self, root):
        self.root = root
        self.results = queue.Queue()
        self._poll()

    def run(self, work, done):
        # Collect leftover Tk objects here on the UI thread. If Python's garbage
        # collector frees them on the worker thread instead, Tk can deadlock.
        gc.collect()

        def target():
            try:
                result, error = work(), None
            except Exception as e:  # reported to the user via `done`
                result, error = None, e
            self.results.put((done, result, error))

        threading.Thread(target=target, daemon=True).start()

    def _poll(self):
        try:
            while True:
                done, result, error = self.results.get_nowait()
                done(result, error)
        except queue.Empty:
            pass
        self.root.after(100, self._poll)


def resource_path(name):
    """Files bundled into the .exe are unpacked to sys._MEIPASS."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def pick_font_family(root):
    available = set(tk.font.families(root))
    for fam in ("Arial Narrow", "Liberation Sans Narrow", "Arial", "Liberation Sans", "Helvetica"):
        if fam in available:
            return fam
    return "TkDefaultFont"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} {VERSION}")
        self.minsize(820, 580)
        try:
            self.iconbitmap(default=resource_path(os.path.join("assets", "icon.ico")))
        except tk.TclError:
            pass  # .ico window icons only work on Windows
        self.settings = load_settings()
        # Show the saved copy of the server's settings straight away; the server
        # itself is read in the background (it can be slow when unreachable).
        self.config_folder = configured_folder()
        if self.config_folder:
            self.cfg = load_cached_config(self.config_folder) or CompanyConfig(source=self.config_folder)
        else:
            self.cfg = load_company_config()
        self.stock_options = []
        self.bg = Background(self)
        self.preview_family = pick_font_family(self)
        self.measure_font = tkinter.font.Font(self, family=self.preview_family, weight="bold")
        self._refresh_pending = False
        self._status_seq = 0

        style = ttk.Style(self)
        style.configure("Print.TButton", font=("Segoe UI", 12, "bold"), padding=(24, 8))
        style.configure("Warn.TLabel", foreground="#b45309")
        style.configure("Ok.TLabel", foreground="#15803d")
        style.configure("Bad.TLabel", foreground="#b91c1c")
        style.configure("Muted.TLabel", foreground="#6b7280")

        self._build()
        self._load_printer_list(select=self.settings.get("last_printer"))
        self.bind_all("<Control-p>", lambda e: self.print_label())
        self.bind_all("<Control-Return>", lambda e: self.print_label())
        self.text.focus_set()
        if self.config_folder:
            self.bg.run(load_company_config, self._config_loaded)
        else:
            self._show_config_problems()
            self._maybe_first_run()

    @property
    def company(self):
        return self.cfg.printers

    @property
    def company_path(self):
        return self.cfg.source

    def _config_loaded(self, cfg, error):
        if error is None:
            self.cfg = cfg
            self._load_printer_list(select=self.printer_var.get() or self.settings.get("last_printer"))
        self._show_config_problems()
        self._maybe_first_run()

    def _show_config_problems(self):
        if self.cfg.problems:
            self.set_status(" ".join(self.cfg.problems), "Warn.TLabel")

    def _maybe_first_run(self):
        if not self.all_printers():
            self.after(300, self._first_run)

    # ----- construction -----

    def _build(self):
        outer = ttk.Frame(self, padding=12)
        outer.pack(fill="both", expand=True)

        # Header: app icon + name
        header = ttk.Frame(outer)
        header.pack(fill="x", pady=(0, 10))
        try:
            big = float(self.tk.call("tk", "scaling")) >= 2.0  # high-DPI screen
            size = "96" if big else "48"
            self.logo = tk.PhotoImage(file=resource_path(os.path.join("assets", f"icon-{size}.png")))
            ttk.Label(header, image=self.logo).pack(side="left", padx=(0, 10))
        except tk.TclError:
            pass  # logo missing; the app works without it
        titles = ttk.Frame(header)
        titles.pack(side="left")
        ttk.Label(titles, text=APP_NAME, font=("Segoe UI", 16, "bold")).pack(anchor="w")
        ttk.Label(titles, text="Zebra label printing", style="Muted.TLabel").pack(anchor="w")
        ttk.Separator(outer).pack(fill="x", pady=(0, 10))

        # Printer row
        top = ttk.Frame(outer)
        top.pack(fill="x")
        ttk.Label(top, text="Printer:").pack(side="left")
        self.printer_var = tk.StringVar()
        self.printer_box = ttk.Combobox(top, textvariable=self.printer_var, state="readonly", width=34)
        self.printer_box.pack(side="left", padx=(6, 8))
        self.printer_box.bind("<<ComboboxSelected>>", lambda e: self._printer_changed())
        ttk.Button(top, text="Check printer", command=self.check_status).pack(side="left")
        ttk.Button(top, text="Manage printers...", command=self.manage_printers).pack(side="left", padx=6)
        self.printer_status = ttk.Label(top, text="", style="Muted.TLabel")
        self.printer_status.pack(side="left", padx=8)

        stock_row = ttk.Frame(outer)
        stock_row.pack(fill="x", pady=(6, 8))
        ttk.Label(stock_row, text="Label stock:").pack(side="left")
        self.stock_box = ttk.Combobox(stock_row, state="readonly", width=34)
        self.stock_box.pack(side="left", padx=(6, 8))
        self.stock_box.bind("<<ComboboxSelected>>", lambda e: self._stock_changed())
        self.size_label = ttk.Label(stock_row, text="", style="Muted.TLabel")
        self.size_label.pack(side="left")

        body = ttk.Frame(outer)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1, uniform="cols")
        body.columnconfigure(1, weight=1, uniform="cols")
        body.rowconfigure(0, weight=1)

        # Left: text + options
        left = ttk.LabelFrame(body, text="Label text (one line per row)", padding=8)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        self.text = tk.Text(left, height=6, width=30, wrap="none", font=("Segoe UI", 13), undo=True)
        self.text.pack(fill="both", expand=True)
        self.text.bind("<<Modified>>", self._text_modified)

        opts = ttk.Frame(left)
        opts.pack(fill="x", pady=(8, 0))

        ttk.Label(opts, text="Text size:").grid(row=0, column=0, sticky="w")
        self.auto_size = tk.BooleanVar(value=True)
        ttk.Radiobutton(opts, text="Auto (as big as fits)", variable=self.auto_size, value=True,
                        command=self._size_mode_changed).grid(row=0, column=1, sticky="w")
        size_row = ttk.Frame(opts)
        size_row.grid(row=1, column=1, sticky="w")
        ttk.Radiobutton(size_row, text="Fixed:", variable=self.auto_size, value=False,
                        command=self._size_mode_changed).pack(side="left")
        self.size_var = tk.StringVar(value="14")
        self.size_spin = ttk.Spinbox(size_row, from_=4, to=200, width=5, textvariable=self.size_var,
                                     command=self.refresh)
        self.size_spin.pack(side="left", padx=4)
        self.size_spin.bind("<KeyRelease>", lambda e: self.refresh())
        ttk.Label(size_row, text="pt").pack(side="left")

        ttk.Label(opts, text="Alignment:").grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.align_var = tk.StringVar(value="center")
        align_row = ttk.Frame(opts)
        align_row.grid(row=2, column=1, sticky="w", pady=(6, 0))
        for value, label in (("left", "Left"), ("center", "Center"), ("right", "Right")):
            ttk.Radiobutton(align_row, text=label, variable=self.align_var, value=value,
                            command=self.refresh).pack(side="left", padx=(0, 8))

        self.emphasize = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="Bigger first line", variable=self.emphasize,
                        command=self.refresh).grid(row=3, column=1, sticky="w", pady=(6, 0))

        ttk.Label(opts, text="Recent:").grid(row=4, column=0, sticky="w", pady=(10, 0))
        recent_row = ttk.Frame(opts)
        recent_row.grid(row=4, column=1, sticky="we", pady=(10, 0))
        self.recent_box = ttk.Combobox(recent_row, state="readonly", width=24)
        self.recent_box.pack(side="left")
        self.recent_box.bind("<<ComboboxSelected>>", lambda e: self._load_recent())
        ttk.Button(recent_row, text="Clear text", command=self._clear_text).pack(side="left", padx=6)
        self._fill_recent()
        self._size_mode_changed(refresh=False)

        # Right: preview
        right = ttk.LabelFrame(body, text="Preview", padding=8)
        right.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        self.canvas = tk.Canvas(right, background="#e5e7eb", highlightthickness=0, width=380, height=280)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda e: self.refresh())
        self.warn_label = ttk.Label(right, text="", style="Warn.TLabel", wraplength=360, justify="left")
        self.warn_label.pack(fill="x", pady=(6, 0))
        ttk.Label(right, text="On-screen preview is approximate.", style="Muted.TLabel").pack(anchor="w")

        # Bottom: copies + print
        bottom = ttk.Frame(outer)
        bottom.pack(fill="x", pady=(12, 0))
        ttk.Label(bottom, text="Copies:", font=("Segoe UI", 11)).pack(side="left")
        self.copies_var = tk.StringVar(value="1")
        ttk.Spinbox(bottom, from_=1, to=MAX_COPIES, width=6, textvariable=self.copies_var,
                    font=("Segoe UI", 11)).pack(side="left", padx=6)
        ttk.Button(bottom, text="Exact preview (online)", command=self.exact_preview).pack(side="left", padx=12)
        self.print_btn = ttk.Button(bottom, text="Print", style="Print.TButton", command=self.print_label)
        self.print_btn.pack(side="right")

        self.status = ttk.Label(outer, text="Ready. Tip: Ctrl+P prints.", style="Muted.TLabel")
        self.status.pack(anchor="w", pady=(8, 0))

    # ----- helpers -----

    def all_printers(self):
        return merge_printers(self.company, self.settings["printers"])

    def base_printer(self):
        name = self.printer_var.get()
        for p in self.all_printers():
            if p.name == name:
                return p
        return None

    def selected_stock(self):
        i = self.stock_box.current()
        return self.stock_options[i] if 0 <= i < len(self.stock_options) else None

    def current_printer(self):
        """The selected printer, sized for the selected label stock."""
        p = self.base_printer()
        st = self.selected_stock()
        if p and st:
            p = replace(p, width_in=st.width_in, height_in=st.height_in)
        return p

    def stock_is_default(self):
        p, st = self.base_printer(), self.selected_stock()
        return not (p and p.stock and st and st.id != p.stock)

    def current_text(self):
        return self.text.get("1.0", "end-1c")

    def current_size(self):
        if self.auto_size.get():
            return None
        try:
            size = float(self.size_var.get())
        except ValueError:
            return None
        return size if size > 0 else None

    def current_layout(self):
        p = self.current_printer()
        if p is None:
            return None
        return layout_label(self.current_text(), p, self.current_size(), self.emphasize.get())

    def set_status(self, text, style="Muted.TLabel"):
        self.status.configure(text=text, style=style)

    def save(self):
        try:
            save_settings(self.settings)
        except OSError as e:
            messagebox.showerror(APP_NAME, f"Could not save settings:\n{e}", parent=self)

    # ----- printers -----

    def _first_run(self):
        messagebox.showinfo(APP_NAME, "Welcome! Let's set up your Zebra printers.\n\n"
                            "Click 'Add from Windows...' to pick printers already installed on this PC, "
                            "then check each one's label size.", parent=self)
        self.manage_printers()

    def _load_printer_list(self, select=None):
        names = [p.name for p in self.all_printers()]
        self.printer_box["values"] = names
        if select in names:
            self.printer_var.set(select)
        elif self.printer_var.get() not in names:
            self.printer_var.set(names[0] if names else "")
        self._printer_changed(remember=False)

    def _fill_stock(self):
        """List the label stock, selecting the printer's default (also used to reset it)."""
        p = self.base_printer()
        self.stock_options, labels = [], []
        if p and not p.stock:
            self.stock_options.append(None)
            labels.append(f"Printer's own size ({p.width_in:g} x {p.height_in:g} in)")
        for st in self.cfg.stock:
            self.stock_options.append(st)
            labels.append(f"{st.name}  (default)" if p and st.id == p.stock else st.name)
        self.stock_box["values"] = labels
        default = next((i for i, st in enumerate(self.stock_options) if st and p and st.id == p.stock), 0)
        if labels:
            self.stock_box.current(default)
        else:
            self.stock_box.set("")
        self._update_info()

    def _stock_changed(self):
        self._update_info()
        self.refresh()

    def _update_info(self):
        p = self.current_printer()
        if not p:
            self.size_label.configure(text="No printer set up yet. Click 'Manage printers...'.",
                                      style="Muted.TLabel")
            return
        text = f"{p.model} via {p.where}, {p.dpi} dpi  |  {p.width_in:g} x {p.height_in:g} in"
        if self.stock_is_default():
            self.size_label.configure(text=text, style="Muted.TLabel")
        else:
            self.size_label.configure(text=text + "  |  one-off stock, resets after printing",
                                      style="Warn.TLabel")

    def _printer_changed(self, remember=True):
        self._fill_stock()
        p = self.current_printer()
        if p:
            if remember and self.settings.get("last_printer") != p.name:
                self.settings["last_printer"] = p.name
                self.save()
            self.check_status(quiet=True)
        else:
            self.printer_status.configure(text="")
        self.refresh()

    def check_status(self, quiet=False):
        p = self.current_printer()
        if not p:
            return
        self._status_seq += 1
        seq = self._status_seq
        self.printer_status.configure(text="Checking...", style="Muted.TLabel")

        def done(result, error):
            if seq != self._status_seq:
                return  # a newer check has started
            if error:
                self.printer_status.configure(text="Not reachable", style="Bad.TLabel")
                if not quiet:
                    messagebox.showwarning(APP_NAME, f"{p.name}: {describe_error(error)}", parent=self)
                return
            problems, diffs = result
            if problems:
                self.printer_status.configure(text=", ".join(problems), style="Bad.TLabel")
            elif diffs:
                self.printer_status.configure(text="Ready (differs from spec)", style="Warn.TLabel")
            else:
                self.printer_status.configure(text="Ready", style="Ok.TLabel")
            if diffs and not quiet:
                messagebox.showinfo(APP_NAME, f"{p.name}'s settings differ from its spec:\n\n- "
                                    + "\n- ".join(diffs) + "\n\nLabels still print; tell IT if they look wrong.",
                                    parent=self)

        self.bg.run(lambda: check_printer(p), done)

    def manage_printers(self):
        PrinterDialog(self)
        gc.collect()  # free the dialog's Tk objects on this thread (see Background.run)
        self._load_printer_list(select=self.printer_var.get())

    # ----- text / options -----

    def _text_modified(self, _event):
        if self.text.edit_modified():
            self.text.edit_modified(False)
            self.refresh()

    def _size_mode_changed(self, refresh=True):
        self.size_spin.configure(state="disabled" if self.auto_size.get() else "normal")
        if refresh:
            self.refresh()

    def _clear_text(self):
        self.text.delete("1.0", "end")
        self.text.focus_set()

    def _fill_recent(self):
        self.recent_box["values"] = [self._recent_title(r) for r in self.settings["recent"]]
        self.recent_box.set("")

    @staticmethod
    def _recent_title(r):
        lines = [ln for ln in r["text"].splitlines() if ln.strip()]
        title = " / ".join(lines)
        return title if len(title) <= 40 else title[:39] + "..."

    def _load_recent(self):
        i = self.recent_box.current()
        if i < 0:
            return
        r = self.settings["recent"][i]
        self.text.delete("1.0", "end")
        self.text.insert("1.0", r["text"])
        self.auto_size.set(r.get("size_pt") is None)
        if r.get("size_pt") is not None:
            self.size_var.set(f"{r['size_pt']:g}")
        self.align_var.set(r.get("align", "center"))
        self.emphasize.set(bool(r.get("emphasize")))
        self._size_mode_changed()
        self.recent_box.set("")

    def _remember(self):
        entry = {
            "text": self.current_text().strip("\n"),
            "size_pt": self.current_size(),
            "align": self.align_var.get(),
            "emphasize": self.emphasize.get(),
        }
        recent = [r for r in self.settings["recent"] if r != entry]
        self.settings["recent"] = [entry] + recent[: MAX_RECENT - 1]
        self.save()
        self._fill_recent()

    # ----- preview -----

    def refresh(self):
        if not self._refresh_pending:
            self._refresh_pending = True
            self.after_idle(self._draw_preview)

    def _draw_preview(self):
        self._refresh_pending = False
        c = self.canvas
        c.delete("all")
        p = self.current_printer()
        cw, ch = max(c.winfo_width(), 50), max(c.winfo_height(), 50)
        if p is None:
            c.create_text(cw / 2, ch / 2, text="Add a printer to get started", fill="#6b7280")
            self.warn_label.configure(text="")
            return
        lay = self.current_layout()
        scale = min((cw - 40) / lay.width, (ch - 40) / lay.height)
        lw, lh = lay.width * scale, lay.height * scale
        x0, y0 = (cw - lw) / 2, (ch - lh) / 2
        c.create_rectangle(x0, y0, x0 + lw, y0 + lh, fill="white", outline="#6b7280")
        m = lay.margin * scale
        c.create_rectangle(x0 + m, y0 + m, x0 + lw - m, y0 + lh - m, outline="#d1d5db", dash=(2, 3))

        align = self.align_var.get()
        anchor, tx = {"left": ("nw", x0 + m), "center": ("n", x0 + lw / 2), "right": ("ne", x0 + lw - m)}[align]
        for ln in lay.lines:
            if not ln.text.strip():
                continue
            px = max(1, round(ln.h * scale))
            # Screen fonts are wider than the printer's condensed font, so shrink
            # to the width the printer will actually use.
            printed_w = text_em(ln.text) * ln.h * scale
            self.measure_font.configure(size=-px)
            screen_w = self.measure_font.measure(ln.text)
            if screen_w > printed_w > 0:
                px = max(1, int(px * printed_w / screen_w))
            c.create_text(tx, y0 + ln.y * scale, text=ln.text, anchor=anchor,
                          font=(self.preview_family, -px, "bold"), fill="black")
        # Anything outside the label won't print, so hide it.
        bg = c.cget("background")
        for box in ((0, 0, cw, y0), (0, y0 + lh, cw, ch), (0, y0, x0, y0 + lh), (x0 + lw, y0, cw, y0 + lh)):
            c.create_rectangle(*box, fill=bg, outline="")
        c.create_rectangle(x0, y0, x0 + lw, y0 + lh, outline="#6b7280")
        if not lay.lines:
            c.create_text(cw / 2, ch / 2, text="Type your label text on the left", fill="#9ca3af")
        self.warn_label.configure(text="\n".join(lay.warnings))

    def exact_preview(self):
        p = self.current_printer()
        lay = self.current_layout()
        if not p or not lay or not lay.lines:
            messagebox.showinfo(APP_NAME, "Pick a printer and type some text first.", parent=self)
            return
        zpl = build_zpl(lay, p, self.align_var.get(), copies=1)
        self.set_status("Getting exact preview from labelary.com...")

        def done(png, error):
            if error:
                self.set_status("Exact preview failed.", "Bad.TLabel")
                messagebox.showerror(APP_NAME, f"Could not get the preview from labelary.com:\n{error}", parent=self)
                return
            self.set_status("Exact preview shown.")
            win = tk.Toplevel(self)
            win.title("Exact preview")
            win.configure(background="#e5e7eb")
            img = tk.PhotoImage(data=png)
            factor = max(1, -(-max(img.width(), img.height()) // 700))  # shrink very large labels
            if factor > 1:
                img = img.subsample(factor)
            lbl = tk.Label(win, image=img, borderwidth=1, relief="solid")
            lbl.image = img
            lbl.pack(padx=20, pady=20)
            ttk.Label(win, text="Rendered by labelary.com from the exact data that will be sent to the printer.",
                      style="Muted.TLabel").pack(padx=20, pady=(0, 12))
            win.transient(self)

        self.bg.run(lambda: labelary_png(zpl, p), done)

    # ----- printing -----

    def print_label(self):
        p = self.current_printer()
        if not p:
            messagebox.showinfo(APP_NAME, "Add a printer first (Manage printers...).", parent=self)
            return
        lay = self.current_layout()
        if not lay.lines:
            messagebox.showinfo(APP_NAME, "Type the label text first.", parent=self)
            return
        try:
            copies = int(self.copies_var.get())
            if not 1 <= copies <= MAX_COPIES:
                raise ValueError
        except ValueError:
            messagebox.showerror(APP_NAME, f"Copies must be a whole number from 1 to {MAX_COPIES}.", parent=self)
            return
        if lay.warnings and not messagebox.askyesno(
                APP_NAME, "\n".join(lay.warnings) + "\n\nPrint anyway?", parent=self):
            return
        if copies > CONFIRM_COPIES_OVER and not messagebox.askyesno(
                APP_NAME, f"Print {copies} labels on {p.name}?", parent=self):
            return

        zpl = build_zpl(lay, p, self.align_var.get(), copies)
        self.print_btn.configure(state="disabled")
        self.set_status(f"Sending to {p.name}...")
        label_word = "label" if copies == 1 else "labels"

        def done(_result, error):
            self.print_btn.configure(state="normal")
            if error:
                self.set_status(f"Print failed: {describe_error(error)}", "Bad.TLabel")
                messagebox.showerror(APP_NAME, f"Could not print on {p.name} ({p.where}).\n\n"
                                     f"{describe_error(error)}", parent=self)
                return
            note = ""
            if not self.stock_is_default():
                self._fill_stock()  # a non-default stock is a one-off; back to the printer's default
                self.refresh()
                note = " Label stock is back to the printer's default."
            self.set_status(f"Sent {copies} {label_word} to {p.name}.{note}", "Ok.TLabel")
            self._remember()
            self.check_status(quiet=True)

        self.bg.run(lambda: send_label(p, zpl.encode("utf-8")), done)


NO_WINDOWS_PRINTER = "(none - send straight to the IP address)"


class PrinterDialog(tk.Toplevel):
    """Add, edit and remove printers. Changes are saved immediately."""

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("Manage printers")
        self.resizable(False, False)
        self.transient(app)
        self.editing = None  # index into app.settings["printers"], or None for a new one
        self.company_selected = False
        self.form_widgets = []
        self.installed = []  # [(Printer guess, is_zebra)] from Windows

        frame = ttk.Frame(self, padding=12)
        frame.pack(fill="both", expand=True)

        left = ttk.Frame(frame)
        left.grid(row=0, column=0, sticky="ns", padx=(0, 12))
        ttk.Label(left, text="Printers in this app").pack(anchor="w")
        self.listbox = tk.Listbox(left, width=30, height=15, exportselection=False)
        self.listbox.pack(fill="y", expand=True)
        self.listbox.bind("<<ListboxSelect>>", lambda e: self._select())
        btns = ttk.Frame(left)
        btns.pack(fill="x", pady=(6, 0))
        self.add_windows_btn = ttk.Button(btns, text="Add from Windows...", command=self._add_from_windows)
        self.add_windows_btn.pack(side="left")
        ttk.Button(btns, text="Add by IP", command=self._new).pack(side="left", padx=4)
        self.delete_btn = ttk.Button(btns, text="Delete", command=self._delete)
        self.delete_btn.pack(side="left")

        form = ttk.Frame(frame)
        form.grid(row=0, column=1, sticky="n")
        self.vars = {k: tk.StringVar() for k in ("name", "windows_name", "host", "port", "model",
                                                 "width_in", "height_in", "dpi", "shift_x", "shift_y")}

        def row(r, label, widget, hint=""):
            ttk.Label(form, text=label).grid(row=r, column=0, sticky="w", pady=3)
            widget.grid(row=r, column=1, sticky="w", pady=3)
            self.form_widgets.append(widget)
            if hint:
                ttk.Label(form, text=hint, style="Muted.TLabel").grid(row=r, column=2, sticky="w", padx=6)

        row(0, "Name", ttk.Entry(form, textvariable=self.vars["name"], width=30), "what you'll see in the list")
        self.windows_box = ttk.Combobox(form, textvariable=self.vars["windows_name"], state="readonly", width=40)
        self.windows_box.bind("<<ComboboxSelected>>", lambda e: self._windows_printer_picked())
        row(1, "Windows printer", self.windows_box)
        row(2, "IP address", ttk.Entry(form, textvariable=self.vars["host"], width=18),
            "optional with a Windows printer;\nused for the ready/out-of-labels check")
        row(3, "Port", ttk.Entry(form, textvariable=self.vars["port"], width=8), "normally 9100")
        model = ttk.Combobox(form, textvariable=self.vars["model"], values=list(MODELS), state="readonly", width=10)
        model.bind("<<ComboboxSelected>>", lambda e: self._model_changed())
        row(4, "Model", model, "fills in the label size")
        row(5, "Label width (in)", ttk.Entry(form, textvariable=self.vars["width_in"], width=8))
        row(6, "Label height (in)", ttk.Entry(form, textvariable=self.vars["height_in"], width=8))
        row(7, "Resolution (dpi)", ttk.Combobox(form, textvariable=self.vars["dpi"],
                                                values=[str(d) for d in DPI_TO_DPMM], state="readonly", width=8),
            "on the printer's sticker")
        row(8, "Shift right (dots)", ttk.Entry(form, textvariable=self.vars["shift_x"], width=8),
            "use negative to move left")
        row(9, "Shift down (dots)", ttk.Entry(form, textvariable=self.vars["shift_y"], width=8),
            "use negative to move up")

        actions = ttk.Frame(form)
        actions.grid(row=10, column=0, columnspan=3, sticky="w", pady=(12, 0))
        self.save_btn = ttk.Button(actions, text="Save printer", command=self._save)
        self.save_btn.pack(side="left")
        ttk.Button(actions, text="Test connection", command=self._test).pack(side="left", padx=6)
        ttk.Button(actions, text="Print test label", command=self._test_label).pack(side="left")
        self.calibrate_btn = ttk.Button(actions, text="Calibrate media", command=self._calibrate)
        self.calibrate_btn.pack(side="left", padx=6)

        self.msg = ttk.Label(form, text="", wraplength=460, justify="left")
        self.msg.grid(row=11, column=0, columnspan=3, sticky="w", pady=(10, 0))

        bottom = ttk.Frame(frame)
        bottom.grid(row=1, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(bottom, text="Close", command=self.destroy).pack()

        self._fill_list()
        if self._rows():
            names = [p.name for _, p in self._rows()]
            current = self.app.printer_var.get()
            self.listbox.selection_set(names.index(current) if current in names else 0)
            self._select()
        else:
            self._new()
            self.msg.configure(text="Click 'Add from Windows...' to pick printers installed on this PC.",
                               style="Muted.TLabel")
        self._load_installed()

        self.grab_set()
        self.wait_window()

    # ----- Windows printers -----

    def _load_installed(self):
        self.windows_box["values"] = [NO_WINDOWS_PRINTER]
        self.add_windows_btn.configure(state="disabled")

        def done(result, error):
            if not self.winfo_exists():
                return
            self.add_windows_btn.configure(state="normal")
            if error:
                self.installed = []
                self.msg.configure(text=f"Couldn't list Windows printers: {describe_error(error)}",
                                   style="Bad.TLabel")
                return
            self.installed = result
            self.windows_box["values"] = [NO_WINDOWS_PRINTER] + [p.name for p, _ in result]

        self.app.bg.run(list_windows_printers, done)

    def _windows_printer_picked(self):
        name = self.vars["windows_name"].get()
        guess = next((p for p, _ in self.installed if p.name == name), None)
        if guess is None:
            return
        if not self.vars["name"].get().strip():
            self.vars["name"].set(guess.name)
        if guess.host and not self.vars["host"].get().strip():
            self.vars["host"].set(guess.host)

    def _add_from_windows(self):
        taken = {p.windows_name for _, p in self._rows() if p.windows_name}
        choices = [(p, z) for p, z in self.installed if p.windows_name not in taken]
        if not self.installed:
            messagebox.showinfo(APP_NAME, "No printers were found in Windows.", parent=self)
            return
        if not choices:
            messagebox.showinfo(APP_NAME, "All the printers installed in Windows are already added.", parent=self)
            return

        win = tk.Toplevel(self)
        win.title("Add printers from Windows")
        win.transient(self)
        body = ttk.Frame(win, padding=12)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Pick the label printers to add (Ctrl+click to pick several).\n"
                             "Zebra printers are listed first.").pack(anchor="w")
        lb = tk.Listbox(body, width=60, height=min(14, max(5, len(choices))), selectmode="extended",
                        exportselection=False)
        lb.pack(fill="both", expand=True, pady=8)
        for i, (p, zebra) in enumerate(choices):
            detail = f"{p.model}, {p.dpi} dpi" if p.model != "Other" else "model unknown"
            lb.insert("end", f"{p.name}   ({detail})" if zebra else f"{p.name}")
            if zebra:
                lb.selection_set(i)
            else:
                lb.itemconfigure(i, foreground="#6b7280")

        def add():
            picked = [choices[i][0] for i in lb.curselection()]
            if not picked:
                return
            printers = self.app.settings["printers"]
            names = {p.name for _, p in self._rows()}
            for p in picked:
                base, n = p.name, 2
                while p.name in names:
                    p.name = f"{base} ({n})"
                    n += 1
                names.add(p.name)
                printers.append(p)
            self.app.save()
            win.destroy()
            self._fill_list()
            self.listbox.selection_clear(0, "end")
            self.listbox.selection_set(len(self.app.company) + len(printers) - len(picked))
            self._select()
            self.msg.configure(
                text=f"Added {len(picked)} printer(s). Check the label size and dpi for each one, "
                     "then 'Print test label'.", style="Ok.TLabel")

        row = ttk.Frame(body)
        row.pack(fill="x")
        ttk.Button(row, text="Add selected", command=add).pack(side="right")
        ttk.Button(row, text="Cancel", command=win.destroy).pack(side="right", padx=6)
        lb.bind("<Double-Button-1>", lambda e: add())
        win.grab_set()
        win.wait_window()

    # ----- list and form -----

    def _rows(self):
        return ([("company", p) for p in self.app.company] +
                [("personal", p) for p in self.app.settings["printers"]])

    def _fill_list(self):
        self.listbox.delete(0, "end")
        for kind, p in self._rows():
            suffix = "  - company" if kind == "company" else ""
            self.listbox.insert("end", f"{p.name}  ({p.width_in:g} x {p.height_in:g} in){suffix}")
            if kind == "company":
                self.listbox.itemconfigure("end", foreground="#1d4ed8")

    def _lock(self, locked):
        """Company printers are shown read-only."""
        for w in self.form_widgets:
            if isinstance(w, ttk.Combobox):
                w.configure(state="disabled" if locked else "readonly")
            else:
                w.configure(state="disabled" if locked else "normal")
        for b in (self.save_btn, self.delete_btn, self.calibrate_btn):
            b.configure(state="disabled" if locked else "normal")

    def _show(self, p):
        for k, v in asdict(p).items():
            if k in self.vars:
                self.vars[k].set(f"{v:g}" if isinstance(v, float) else str(v))
        if not p.windows_name:
            self.vars["windows_name"].set(NO_WINDOWS_PRINTER)

    def _select(self):
        sel = self.listbox.curselection()
        if not sel:
            return
        kind, p = self._rows()[sel[0]]
        self.company_selected = kind == "company"
        self.editing = None if self.company_selected else sel[0] - len(self.app.company)
        self._lock(self.company_selected)
        self._show(p)
        if self.company_selected:
            self.msg.configure(text=f"Company printer, set by IT in {self.app.company_path}. "
                                    "It can't be changed here.", style="Muted.TLabel")
        else:
            self.msg.configure(text="", style="TLabel")

    def _new(self):
        self.editing = None
        self.company_selected = False
        self._lock(False)
        self.listbox.selection_clear(0, "end")
        self._show(Printer(name=""))
        self.msg.configure(text="Fill in the details and click 'Save printer'.", style="Muted.TLabel")

    def _model_changed(self):
        w, h = MODELS[self.vars["model"].get()]
        self.vars["width_in"].set(f"{w:g}")
        self.vars["height_in"].set(f"{h:g}")

    def _read_form(self):
        v = {k: var.get().strip() for k, var in self.vars.items()}
        windows_name = "" if v["windows_name"] == NO_WINDOWS_PRINTER else v["windows_name"]
        if not v["name"]:
            raise ValueError("Give the printer a name.")
        if not windows_name and not v["host"]:
            raise ValueError("Pick a Windows printer or enter the printer's IP address.")
        try:
            port = int(v["port"] or 9100)
            assert 1 <= port <= 65535
        except (ValueError, AssertionError):
            raise ValueError("Port must be a number (normally 9100).")
        try:
            w, h = float(v["width_in"]), float(v["height_in"])
            assert 0.25 <= w <= 20 and 0.25 <= h <= 40
        except (ValueError, AssertionError):
            raise ValueError("Label width and height must be in inches, e.g. 2 and 1.")
        try:
            sx, sy = int(v["shift_x"] or 0), int(v["shift_y"] or 0)
            assert -500 <= sx <= 500 and -500 <= sy <= 500
        except (ValueError, AssertionError):
            raise ValueError("Shift must be a whole number of dots between -500 and 500.")
        return Printer(name=v["name"], host=v["host"], port=port, windows_name=windows_name,
                       model=v["model"] or "Other", width_in=w, height_in=h, dpi=int(v["dpi"] or 203),
                       shift_x=sx, shift_y=sy)

    def _form_printer(self):
        try:
            return self._read_form()
        except ValueError as e:
            self.msg.configure(text=str(e), style="Bad.TLabel")
            return None

    def _save(self):
        if self.company_selected:
            return
        p = self._form_printer()
        if not p:
            return
        if any(c.name == p.name for c in self.app.company):
            self.msg.configure(text="A company printer already has that name.", style="Bad.TLabel")
            return
        printers = self.app.settings["printers"]
        for i, other in enumerate(printers):
            if other.name == p.name and i != self.editing:
                self.msg.configure(text="Another printer already has that name.", style="Bad.TLabel")
                return
        if self.editing is None:
            printers.append(p)
            self.editing = len(printers) - 1
        else:
            old_name = printers[self.editing].name
            printers[self.editing] = p
            if self.app.settings.get("last_printer") == old_name:
                self.app.settings["last_printer"] = p.name
            if self.app.printer_var.get() == old_name:
                self.app.printer_var.set(p.name)
        self.app.save()
        self._fill_list()
        self.listbox.selection_set(len(self.app.company) + self.editing)
        self.msg.configure(text=f"Saved {p.name}.", style="Ok.TLabel")

    def _delete(self):
        if self.editing is None or self.company_selected:
            return
        p = self.app.settings["printers"][self.editing]
        if not messagebox.askyesno(APP_NAME, f"Remove {p.name} from this app?\n\n"
                                   "(It stays installed in Windows.)", parent=self):
            return
        del self.app.settings["printers"][self.editing]
        self.app.save()
        self._fill_list()
        self._new()

    # ----- actions -----

    def _run(self, p, what, work, ok_text):
        self.msg.configure(text=f"{what} {p.name}...", style="Muted.TLabel")

        def done(result, error):
            if not self.winfo_exists():
                return
            if error:
                self.msg.configure(text=describe_error(error), style="Bad.TLabel")
            else:
                text, style = ok_text(result) if callable(ok_text) else ok_text
                self.msg.configure(text=text, style=style)

        self.app.bg.run(work, done)

    def _test(self):
        p = self._form_printer()
        if not p:
            return

        def ok(problems):
            if problems:
                return f"Connected, but the printer reports: {', '.join(problems)}.", "Warn.TLabel"
            if not p.host:
                return ("Windows says the printer is ready. Add its IP address for a more reliable "
                        "check.", "Ok.TLabel")
            return "Connected. The printer is ready.", "Ok.TLabel"

        self._run(p, "Contacting", lambda: printer_problems(p), ok)

    def _test_label(self):
        p = self._form_printer()
        if not p:
            return
        text = f"{p.name}\n{p.width_in:g} x {p.height_in:g} in, {p.dpi} dpi"
        zpl = build_zpl(layout_label(text, p), p, "center", 1, border=True)
        self._run(p, "Printing a test label on", lambda: send_label(p, zpl.encode("utf-8")),
                  ("Test label sent. The box should line up with the label's edges. "
                   "If it's off, adjust Shift and save.", "Ok.TLabel"))

    def _calibrate(self):
        p = self._form_printer()
        if not p:
            return
        if not messagebox.askyesno(APP_NAME, f"Calibrate {p.name}?\n\nThe printer will feed a few blank labels "
                                   "to measure them. Do this after loading a new kind of label.", parent=self):
            return
        self._run(p, "Calibrating", lambda: send_label(p, b"~JC"), ("Calibration started.", "Ok.TLabel"))


def main():
    if sys.platform == "win32":
        try:  # sharp text on high-DPI screens
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    App().mainloop()


if __name__ == "__main__":
    main()
