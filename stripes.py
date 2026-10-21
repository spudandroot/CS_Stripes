"""Stripes - type some text, pick a printer, print labels on Zebra printers.

Sends raw ZPL to Zebra printers, either through a printer installed in
Windows (via the print spooler) or straight to an IP address on port 9100.
Needs pywin32 for Windows printers; everything else is standard library.
"""

import copy
import gc
import glob
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import queue
import urllib.request
from dataclasses import dataclass, asdict, field, fields, replace
from datetime import datetime

import tkinter as tk
import tkinter.font
from tkinter import ttk, messagebox, filedialog

import label_design as ld

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

MAX_RECENT = 20
MAX_COPIES = 9999
CONFIRM_COPIES_OVER = 50
UPDATE_URL = "https://github.com/spudandroot/CS_Stripes/releases"
UPDATE_CHECK_HOURS = 6


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

def label_dots(printer):
    dots_per_inch = printer.dpmm * 25.4
    return round(printer.width_in * dots_per_inch), round(printer.height_in * dots_per_inch)


def printer_label(printer):
    return ld.Label(*label_dots(printer), printer.dpmm)


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
# Self-update
# --------------------------------------------------------------------------

def parse_version(text):
    """'v1.2.3' -> (1, 2, 3); None for anything else (e.g. development builds)."""
    m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", (text or "").strip())
    return tuple(int(n) for n in m.groups()) if m else None


def _fetch(url, timeout=60):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


def check_for_update(current=None, fetch=_fetch):
    """The newest release's version if it's newer than this one, else None."""
    mine = parse_version(VERSION if current is None else current)
    if mine is None:
        return None
    latest = fetch(f"{UPDATE_URL}/latest/download/version.txt").decode("ascii", "replace").strip()
    theirs = parse_version(latest)
    return latest if theirs and theirs > mine else None


def folder_is_writable(folder):
    try:
        with tempfile.TemporaryFile(dir=folder):
            return True
    except OSError:
        return False


def install_update(version, exe_path, fetch=_fetch):
    """Download a release and put it in place of exe_path, for the next start.

    Windows lets a running program be renamed but not overwritten, so the
    running exe is moved aside (Stripes.exe.old-...) and the new one takes its
    name. Old copies are cleaned up on a later start.
    """
    base = f"{UPDATE_URL}/download/{version}"
    data = fetch(f"{base}/Stripes.exe", timeout=600)
    expected = fetch(f"{base}/Stripes.exe.sha256").decode("ascii", "replace").strip().upper()
    if hashlib.sha256(data).hexdigest().upper() != expected:
        raise ValueError("the download was damaged (checksum mismatch)")
    new = exe_path + ".new"
    with open(new, "wb") as f:
        f.write(data)
    if os.path.exists(exe_path):
        os.replace(exe_path, f"{exe_path}.old-{int(time.time() * 1000)}")
    os.replace(new, exe_path)


def remove_old_versions(exe_path):
    for old in glob.glob(glob.escape(exe_path) + ".old-*") + [exe_path + ".new"]:
        try:
            os.remove(old)
        except OSError:
            pass  # still running in another window; next time


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
        self.minsize(900, 640)
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
        self.design = ld.Design([ld.Element("text")])
        self.selected = []
        self._loading_props = False
        self._syncing = False
        self._drag = None
        self._view = None
        self._preview_images = []

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
        self._fill_tree(select=[0])
        self.simple_box.focus_set()
        if self.config_folder:
            self.bg.run(load_company_config, self._config_loaded)
        else:
            self._show_config_problems()
            self._maybe_first_run()
        if getattr(sys, "frozen", False):  # only the built .exe updates itself
            remove_old_versions(sys.executable)
            self.after(3000, self._check_for_update)

    # ----- self-update -----

    def _check_for_update(self):
        exe = sys.executable

        def work():
            latest = check_for_update()
            if not latest:
                return None
            if not folder_is_writable(os.path.dirname(exe)):
                return "available", latest
            install_update(latest, exe)
            return "installed", latest

        def done(result, error):
            if error or not result:
                # offline or nothing new: look again later
                self.after(UPDATE_CHECK_HOURS * 3600 * 1000, self._check_for_update)
                return
            kind, version = result
            if kind == "installed":
                self.update_label.configure(text=f"Stripes {version} is ready.", style="Ok.TLabel")
                self.update_button.pack(side="right", padx=(8, 0), before=self.update_label)
            else:
                self.update_label.configure(text=f"Stripes {version} is available; this copy can't update itself.",
                                            style="Warn.TLabel")

        self.bg.run(work, done)

    def _restart_to_update(self):
        if not self.active_design().is_empty() and not messagebox.askyesno(
                APP_NAME, "Restart Stripes now? The label you're working on will be cleared.", parent=self):
            return
        # PYINSTALLER_RESET_ENVIRONMENT makes the new copy start fresh rather
        # than reuse this one's unpacked files.
        env = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT="1")
        subprocess.Popen([sys.executable], env=env, close_fds=True)
        self.destroy()

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
        self.update_label = ttk.Label(header, text=f"Version {VERSION}", style="Muted.TLabel")
        self.update_label.pack(side="right")
        self.update_button = ttk.Button(header, text="Restart to update", command=self._restart_to_update)
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
        body.columnconfigure(0, weight=2, uniform="cols")
        body.columnconfigure(1, weight=3, uniform="cols")
        body.rowconfigure(0, weight=1)

        # Left: Simple tab (just type) and Advanced tab (items, barcodes, pictures...)
        left = ttk.Frame(body)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        self.tabs = ttk.Notebook(left)
        self.tabs.pack(fill="both", expand=True)
        simple, advanced = ttk.Frame(self.tabs, padding=8), ttk.Frame(self.tabs, padding=8)
        self.tabs.add(simple, text="  Simple  ")
        self.tabs.add(advanced, text="  Advanced  ")
        self._build_simple(simple)
        self._build_items(advanced)
        self.tabs.bind("<<NotebookTabChanged>>", self._tab_changed)
        recent_row = ttk.Frame(left)
        recent_row.pack(fill="x", pady=(8, 0))
        ttk.Label(recent_row, text="Recent:").pack(side="left")
        self.recent_box = ttk.Combobox(recent_row, state="readonly")
        self.recent_box.pack(side="left", fill="x", expand=True, padx=6)
        self.recent_box.bind("<<ComboboxSelected>>", lambda e: self._load_recent())
        ttk.Button(recent_row, text="New label", command=self._new_label).pack(side="left")
        self._fill_recent()

        right = self.preview_frame = ttk.LabelFrame(body, text="Preview", padding=8)
        right.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        self.canvas = tk.Canvas(right, background="#e5e7eb", highlightthickness=0, width=420, height=300)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda e: self.refresh())
        self.canvas.bind("<ButtonPress-1>", self._canvas_press)
        self.canvas.bind("<B1-Motion>", self._canvas_drag)
        self.canvas.bind("<ButtonRelease-1>", lambda e: setattr(self, "_drag", None))
        self.canvas.bind("<Delete>", lambda e: self._delete_items())
        self.warn_label = ttk.Label(right, text="", style="Warn.TLabel", wraplength=420, justify="left")
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

    def _build_simple(self, parent):
        ttk.Label(parent, text="Type your label, one line per row:").pack(anchor="w")
        self.simple_box = tk.Text(parent, height=8, width=30, wrap="none", font=("Segoe UI", 14), undo=True)
        self.simple_box.pack(fill="both", expand=True, pady=(4, 0))
        self.simple_box.bind("<<Modified>>", self._simple_modified)
        self.simple_box.bind("<Control-Return>", lambda e: (self.print_label(), "break")[1])
        ttk.Label(parent, text="Text is sized to fill the label. For barcodes, QR codes, pictures or a different "
                               "size per line, use the Advanced tab.",
                  style="Muted.TLabel", wraplength=330, justify="left").pack(anchor="w", pady=(6, 0))

    def _build_items(self, parent):
        tree_frame = ttk.Frame(parent)
        tree_frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(tree_frame, columns=("item", "size", "align"), show="headings",
                                 selectmode="extended", height=6)
        for col, title, width, stretch in (("item", "Item", 200, True), ("size", "Size", 60, False),
                                           ("align", "Align", 60, False)):
            self.tree.heading(col, text=title, anchor="w")
            self.tree.column(col, width=width, stretch=stretch, anchor="w")
        scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._tree_selected)
        self.tree.bind("<Delete>", lambda e: self._delete_items())

        add_row = ttk.Frame(parent)
        add_row.pack(fill="x", pady=(6, 0))
        for text, kind in (("+ Text", "text"), ("+ Barcode", "barcode"), ("+ QR code", "qr"), ("+ Picture", "image")):
            ttk.Button(add_row, text=text, command=lambda k=kind: self._add_item(k)).pack(side="left", padx=(0, 4))
        move_row = ttk.Frame(parent)
        move_row.pack(fill="x", pady=(4, 0))
        ttk.Button(move_row, text="Move up", command=lambda: self._move_items(-1)).pack(side="left")
        ttk.Button(move_row, text="Move down", command=lambda: self._move_items(1)).pack(side="left", padx=4)
        ttk.Button(move_row, text="Delete", command=self._delete_items).pack(side="left")

        ttk.Separator(parent).pack(fill="x", pady=8)

        # Settings for the selected item(s); rows are shown to suit what's selected.
        self.props = ttk.Frame(parent)
        self.props.pack(fill="x")
        self.v_value = tk.StringVar()
        self.v_size_auto = tk.BooleanVar(value=True)
        self.v_size = tk.StringVar(value="14")
        self.v_align = tk.StringVar(value="center")
        self.v_symbology = tk.StringVar()
        self.v_show_text = tk.BooleanVar(value=True)
        self.v_dither = tk.BooleanVar(value=False)
        for name in ("value", "size_auto", "size", "align", "symbology", "show_text", "dither"):
            getattr(self, f"v_{name}").trace_add("write", lambda *a, n=name: self._prop_changed(n))

        self.row_value = ttk.Frame(self.props)
        self.value_label = ttk.Label(self.row_value, text="Text:", width=7)
        self.value_label.pack(side="left")
        self.value_entry = ttk.Entry(self.row_value, textvariable=self.v_value, font=("Segoe UI", 12))
        self.value_entry.pack(side="left", fill="x", expand=True)
        self.value_entry.bind("<Return>", self._enter_pressed)
        self.value_entry.bind("<Up>", lambda e: self._step_selection(-1))
        self.value_entry.bind("<Down>", lambda e: self._step_selection(1))

        self.row_size = ttk.Frame(self.props)
        ttk.Label(self.row_size, text="Size:", width=7).pack(side="left")
        ttk.Radiobutton(self.row_size, text="Fit", variable=self.v_size_auto, value=True).pack(side="left")
        ttk.Radiobutton(self.row_size, text="Fixed:", variable=self.v_size_auto, value=False).pack(side="left",
                                                                                                  padx=(8, 0))
        self.size_spin = ttk.Spinbox(self.row_size, from_=4, to=200, width=5, textvariable=self.v_size)
        self.size_spin.pack(side="left", padx=4)
        ttk.Label(self.row_size, text="pt").pack(side="left")

        self.row_code = ttk.Frame(self.props)
        ttk.Label(self.row_code, text="Type:", width=7).pack(side="left")
        ttk.Combobox(self.row_code, textvariable=self.v_symbology, values=list(ld.SYMBOLOGIES.values()),
                     state="readonly", width=10).pack(side="left")
        ttk.Checkbutton(self.row_code, text="Show digits", variable=self.v_show_text).pack(side="left", padx=8)

        self.row_picture = ttk.Frame(self.props)
        ttk.Label(self.row_picture, text="Picture:", width=7).pack(side="left")
        ttk.Button(self.row_picture, text="Change...", command=self._change_picture).pack(side="left")
        ttk.Checkbutton(self.row_picture, text="Dither (for photos)", variable=self.v_dither).pack(side="left",
                                                                                                padx=8)

        self.row_align = ttk.Frame(self.props)
        ttk.Label(self.row_align, text="Align:", width=7).pack(side="left")
        for value, text in (("left", "Left"), ("center", "Center"), ("right", "Right")):
            ttk.Radiobutton(self.row_align, text=text, variable=self.v_align, value=value).pack(side="left",
                                                                                                padx=(0, 8))
        self.props_hint = ttk.Label(self.props, text="Add an item, or select one to change it.\n"
                                                     "Ctrl+click or Shift+click to select several.",
                                    style="Muted.TLabel")

        options = ttk.Frame(parent)
        options.pack(fill="x", pady=(10, 0))
        self.v_auto_layout = tk.BooleanVar(value=True)
        ttk.Checkbutton(options, text="Auto layout (stack items to fill the label)", variable=self.v_auto_layout,
                        command=self._auto_layout_toggled).pack(side="left")

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

    def in_simple_mode(self):
        return self.tabs.index(self.tabs.select()) == 0

    def active_design(self):
        """What would print right now: the Simple tab's text, or the Advanced tab's items."""
        if self.in_simple_mode():
            return ld.Design.from_text(self.simple_box.get("1.0", "end-1c"))
        return self.design

    def current_label(self):
        p = self.current_printer()
        return printer_label(p) if p else None

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

    # ----- label items -----

    KIND_NAMES = {"text": "Text", "barcode": "Barcode", "qr": "QR code", "image": "Picture"}
    NEW_ITEM_IN = {"text": 0.25, "barcode": 0.5, "qr": 0.8, "image": 0.6}  # starting height, manual layout

    def selected_elements(self):
        return [self.design.elements[i] for i in self.selected if i < len(self.design.elements)]

    def _row_values(self, e):
        if e.kind == "text":
            item = e.value or "(blank line)"
            size = "Fit" if e.size_pt is None else f"{e.size_pt:g} pt"
        else:
            item, size = f"{self.KIND_NAMES[e.kind]}: {e.value}" if e.kind != "image" else "Picture", ""
        return item, size, e.align.capitalize()

    def _fill_tree(self, select=None):
        if select is not None:
            self.selected = [i for i in select if 0 <= i < len(self.design.elements)]
        self.tree.delete(*self.tree.get_children())
        for i, e in enumerate(self.design.elements):
            self.tree.insert("", "end", iid=str(i), values=self._row_values(e))
        self._sync_tree_selection()

    def _sync_tree_selection(self):
        self._syncing = True
        self.tree.selection_set([str(i) for i in self.selected])
        if self.selected:
            self.tree.see(str(self.selected[0]))
        self._syncing = False
        self._show_props()
        self.refresh()

    def _tree_selected(self, _event):
        chosen = sorted(int(i) for i in self.tree.selection())
        if chosen == self.selected:
            return
        self.selected = chosen
        self._show_props()
        self.refresh()

    def _show_props(self):
        for row in (self.row_value, self.row_size, self.row_code, self.row_picture, self.row_align,
                    self.props_hint):
            row.pack_forget()
        els = self.selected_elements()
        if not els:
            self.props_hint.pack(anchor="w")
            return
        first, single = els[0], len(els) == 1
        self._loading_props = True
        try:
            if single and first.kind != "image":
                self.value_label.configure(text="Text:" if first.kind == "text" else "Data:")
                self.v_value.set(first.value)
                self.row_value.pack(fill="x", pady=2)
            if all(e.kind == "text" for e in els):
                self.v_size_auto.set(first.size_pt is None)
                if first.size_pt:
                    self.v_size.set(f"{first.size_pt:g}")
                self.size_spin.configure(state="disabled" if first.size_pt is None else "normal")
                self.row_size.pack(fill="x", pady=2)
            if single and first.kind == "barcode":
                self.v_symbology.set(ld.SYMBOLOGIES[first.symbology])
                self.v_show_text.set(first.show_text)
                self.row_code.pack(fill="x", pady=2)
            if single and first.kind == "image":
                self.v_dither.set(first.dither)
                self.row_picture.pack(fill="x", pady=2)
            self.v_align.set(first.align)
            self.row_align.pack(fill="x", pady=2)
        finally:
            self._loading_props = False

    def _prop_changed(self, name):
        if self._loading_props:
            return
        els = self.selected_elements()
        if not els:
            return
        if name == "value" and len(els) == 1:
            els[0].value = self.v_value.get()
        elif name in ("size_auto", "size"):
            auto = self.v_size_auto.get()
            self.size_spin.configure(state="disabled" if auto else "normal")
            try:
                size = None if auto else float(self.v_size.get())
            except ValueError:
                return  # still typing
            if size is not None and not 1 <= size <= 500:
                return
            for e in els:
                if e.kind == "text":
                    e.size_pt = size
        elif name == "align":
            for e in els:
                e.align = self.v_align.get()
        elif name == "symbology":
            names = {v: k for k, v in ld.SYMBOLOGIES.items()}
            els[0].symbology = names.get(self.v_symbology.get(), "code128")
        elif name == "show_text":
            els[0].show_text = self.v_show_text.get()
        elif name == "dither":
            els[0].dither = self.v_dither.get()
        for i in self.selected:
            self.tree.item(str(i), values=self._row_values(self.design.elements[i]))
        self.refresh()

    def _choose_picture(self):
        path = filedialog.askopenfilename(
            parent=self, title="Choose a picture",
            filetypes=[("Pictures", "*.png *.jpg *.jpeg *.bmp *.gif *.tif *.tiff"), ("All files", "*.*")])
        if not path:
            return None
        try:
            return ld.load_picture(path)
        except Exception as e:  # any unreadable file
            messagebox.showerror(APP_NAME, f"Couldn't open that picture:\n{e}", parent=self)
            return None

    def _add_item(self, kind, focus=True):
        element = ld.Element(kind)
        if kind == "image":
            element.image_png = self._choose_picture()
            if not element.image_png:
                return
        label = self.current_label()
        if not self.design.auto_layout and label:
            m = label.margin
            h = int(min(label.dots(self.NEW_ITEM_IN[kind]), label.height - 2 * m))
            element.x, element.y, element.w, element.h = m, m, label.width - 2 * m, h
            if kind == "qr":
                element.w = h
        at = max(self.selected) + 1 if self.selected else len(self.design.elements)
        self.design.elements.insert(at, element)
        self._fill_tree(select=[at])
        if focus and kind != "image":
            self.value_entry.focus_set()

    def _enter_pressed(self, event):
        if event.state & 0x4:  # Ctrl+Enter prints instead
            return None
        self._add_item("text")
        return "break"

    def _step_selection(self, delta):
        if len(self.selected) == 1:
            i = self.selected[0] + delta
            if 0 <= i < len(self.design.elements):
                self._fill_tree(select=[i])
                self.value_entry.focus_set()
                self.value_entry.icursor("end")
        return "break"

    def _move_items(self, delta):
        els, chosen = self.design.elements, sorted(self.selected)
        if not chosen or (delta < 0 and chosen[0] == 0) or (delta > 0 and chosen[-1] == len(els) - 1):
            return
        for i in (chosen if delta < 0 else reversed(chosen)):
            els[i], els[i + delta] = els[i + delta], els[i]
        self._fill_tree(select=[i + delta for i in chosen])

    def _delete_items(self):
        if not self.selected:
            return
        first = min(self.selected)
        for i in sorted(self.selected, reverse=True):
            del self.design.elements[i]
        self._fill_tree(select=[min(first, len(self.design.elements) - 1)] if self.design.elements else [])

    def _change_picture(self):
        els = self.selected_elements()
        png = self._choose_picture() if els else None
        if png:
            els[0].image_png = png
            self.refresh()

    def _auto_layout_toggled(self):
        if self.v_auto_layout.get():
            self.design.auto_layout = True
        else:
            self._switch_to_manual()
        self.refresh()

    def _switch_to_manual(self):
        """Keep everything exactly where auto layout put it, in boxes that fit the content."""
        label = self.current_label()
        if label and self.design.auto_layout:
            ld.tighten(self.design, ld.render(self.design, label))
        self.design.auto_layout = False
        self.v_auto_layout.set(False)

    def _new_label(self):
        if self.in_simple_mode():
            self.simple_box.delete("1.0", "end")
            self.simple_box.focus_set()
            return
        self.design = ld.Design([ld.Element("text")])
        self.v_auto_layout.set(True)
        self._fill_tree(select=[0])
        self.value_entry.focus_set()

    def _simple_modified(self, _event):
        if self.simple_box.edit_modified():
            self.simple_box.edit_modified(False)
            self.refresh()

    def _tab_changed(self, _event=None):
        if self.in_simple_mode():
            self.preview_frame.configure(text="Preview")
            self.simple_box.focus_set()
        else:
            # First time in Advanced: start from what was typed, to refine it.
            typed = ld.Design.from_text(self.simple_box.get("1.0", "end-1c"))
            if self.design.is_empty() and not typed.is_empty():
                self.design = typed
                self.v_auto_layout.set(True)
                self._fill_tree(select=[0])
            self.preview_frame.configure(text="Preview: drag items to move them, drag the corner to resize")
            self.value_entry.focus_set()
        self.refresh()

    def _fill_recent(self):
        self.recent_box["values"] = [self._recent_title(r) for r in self.settings["recent"]]
        self.recent_box.set("")

    @staticmethod
    def _recent_title(r):
        try:
            design = ld.Design.from_dict(r)
        except (TypeError, ValueError, AttributeError):
            return "(unreadable)"
        parts = [e.title for e in design.elements if e.kind != "text" or e.value.strip()]
        title = " / ".join(parts) or "(empty)"
        return title if len(title) <= 50 else title[:49] + "..."

    def _load_recent(self):
        i = self.recent_box.current()
        if i < 0:
            return
        try:
            design = ld.Design.from_dict(copy.deepcopy(self.settings["recent"][i]))
        except (TypeError, ValueError, AttributeError):
            messagebox.showerror(APP_NAME, "That recent label couldn't be loaded.", parent=self)
            return
        self.recent_box.set("")
        text = design.simple_text()
        if text is not None:  # plain typed text: open it in the Simple tab
            self.simple_box.delete("1.0", "end")
            self.simple_box.insert("1.0", text)
            self.tabs.select(0)
            return
        self.design = design
        self.v_auto_layout.set(design.auto_layout)
        self._fill_tree(select=[0] if design.elements else [])
        self.tabs.select(1)

    def _remember(self, design):
        entry = design.to_dict()
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
        self._preview_images = []
        cw, ch = max(c.winfo_width(), 50), max(c.winfo_height(), 50)
        label = self.current_label()
        if label is None:
            self._view = None
            c.create_text(cw / 2, ch / 2, text="Add a printer to get started", fill="#6b7280")
            self.warn_label.configure(text="")
            return
        design = self.active_design()
        rendered = ld.render(design, label)
        scale = min((cw - 40) / label.width, (ch - 40) / label.height)
        lw, lh = label.width * scale, label.height * scale
        x0, y0 = (cw - lw) / 2, (ch - lh) / 2
        self._view = (x0, y0, scale)
        c.create_rectangle(x0, y0, x0 + lw, y0 + lh, fill="white", outline="#6b7280")
        m = label.margin * scale
        c.create_rectangle(x0 + m, y0 + m, x0 + lw - m, y0 + lh - m, outline="#e5e7eb", dash=(2, 3))
        for mark in rendered.marks:
            self._draw_mark(mark, x0, y0, scale)

        # Anything outside the label won't print, so hide it.
        bg = c.cget("background")
        for box in ((0, 0, cw, y0), (0, y0 + lh, cw, ch), (0, y0, x0, y0 + lh), (x0 + lw, y0, cw, y0 + lh)):
            c.create_rectangle(*box, fill=bg, outline="")
        c.create_rectangle(x0, y0, x0 + lw, y0 + lh, outline="#6b7280")

        for i in ([] if self.in_simple_mode() else self.selected):
            if i >= len(self.design.elements):
                continue
            e = self.design.elements[i]
            bx, by, bw, bh = x0 + e.x * scale, y0 + e.y * scale, e.w * scale, e.h * scale
            c.create_rectangle(bx, by, bx + bw, by + bh, outline="#2563eb", dash=(4, 2))
            if len(self.selected) == 1:
                c.create_rectangle(bx + bw - 4, by + bh - 4, bx + bw + 4, by + bh + 4, fill="#2563eb", outline="")
        if design.is_empty():
            c.create_text(cw / 2, ch / 2, text="Type your label text on the left", fill="#9ca3af")
        problems = rendered.errors + rendered.warnings
        self.warn_label.configure(text="\n".join(problems), style="Bad.TLabel" if rendered.errors else "Warn.TLabel")

    def _draw_mark(self, mark, x0, y0, scale):
        from PIL import Image, ImageTk
        c = self.canvas
        x, y = x0 + mark.x * scale, y0 + mark.y * scale
        if mark.kind == "text":
            px = max(1, round(mark.text_h * scale))
            # Screen fonts are wider than the printer's condensed font, so shrink
            # to the width the printer will actually use.
            printed_w = ld.text_em(mark.text) * mark.text_h * scale
            self.measure_font.configure(size=-px)
            screen_w = self.measure_font.measure(mark.text)
            if screen_w > printed_w > 0:
                px = max(1, int(px * printed_w / screen_w))
            anchor, tx = {"left": ("nw", x), "center": ("n", x + mark.w * scale / 2),
                          "right": ("ne", x + mark.w * scale)}[mark.align]
            c.create_text(tx, y, text=mark.text, anchor=anchor, font=(self.preview_family, -px, "bold"),
                          fill="black")
        elif mark.kind == "bars":
            start = None
            for i, bar in enumerate(mark.pattern + [False]):
                if bar and start is None:
                    start = i
                elif not bar and start is not None:
                    c.create_rectangle(x + start * mark.module * scale, y, x + i * mark.module * scale,
                                       y + mark.h * scale, fill="black", outline="")
                    start = None
        else:
            if mark.kind == "qr":
                n = len(mark.pattern)
                img = Image.new("L", (n, n), 255)
                img.putdata([0 if v else 255 for row in mark.pattern for v in row])
                resample = Image.NEAREST
            else:
                img, resample = mark.bitmap.convert("L"), Image.BILINEAR
            size = (max(1, round(mark.w * scale)), max(1, round(mark.h * scale)))
            photo = ImageTk.PhotoImage(img.resize(size, resample), master=c)
            self._preview_images.append(photo)  # Tk only keeps a weak reference
            c.create_image(x, y, image=photo, anchor="nw")

    # ----- dragging on the preview -----

    def _canvas_press(self, event):
        self._drag = None
        if not self._view or self.in_simple_mode():
            return
        self.canvas.focus_set()
        x0, y0, scale = self._view
        x, y = (event.x - x0) / scale, (event.y - y0) / scale
        els = self.design.elements
        if len(self.selected) == 1 and self.selected[0] < len(els):
            e = els[self.selected[0]]
            if abs(x - (e.x + e.w)) <= 8 / scale and abs(y - (e.y + e.h)) <= 8 / scale:
                self._drag = ("resize", x, y, {self.selected[0]: (e.x, e.y, e.w, e.h)})
                return
        hit = next((i for i in reversed(range(len(els)))
                    if els[i].x <= x <= els[i].x + els[i].w and els[i].y <= y <= els[i].y + els[i].h), None)
        if hit is None:
            return
        if event.state & 0x5:  # Shift or Ctrl: add to / remove from the selection
            self.selected = sorted(set(self.selected) ^ {hit})
        elif hit not in self.selected:
            self.selected = [hit]
        self._sync_tree_selection()
        self._drag = ("move", x, y, {i: (els[i].x, els[i].y, els[i].w, els[i].h) for i in self.selected})

    def _canvas_drag(self, event):
        if not self._drag or not self._view:
            return
        x0, y0, scale = self._view
        mode, sx, sy, start = self._drag
        dx, dy = round((event.x - x0) / scale - sx), round((event.y - y0) / scale - sy)
        if not (dx or dy):
            return
        if self.design.auto_layout:  # moving something by hand: keep everything where it is from now on
            self._switch_to_manual()
            start = {i: (e.x, e.y, e.w, e.h) for i, e in
                     ((i, self.design.elements[i]) for i in start)}
            self._drag = (mode, sx, sy, start)
        label = self.current_label()
        for i, (ex, ey, ew, eh) in start.items():
            e = self.design.elements[i]
            if mode == "move":
                e.x = min(max(0, ex + dx), label.width - e.w)
                e.y = min(max(0, ey + dy), label.height - e.h)
                if len(start) == 1 and abs(e.x + e.w / 2 - label.width / 2) < 6:
                    e.x = round((label.width - e.w) / 2)  # snap to the middle of the label
            else:
                e.w = min(max(12, ew + dx), label.width - e.x)
                e.h = min(max(12, eh + dy), label.height - e.y)
        self.refresh()

    def exact_preview(self):
        p, label, design = self.current_printer(), self.current_label(), self.active_design()
        if not p or design.is_empty():
            messagebox.showinfo(APP_NAME, "Pick a printer and add something to the label first.", parent=self)
            return
        zpl = ld.build_zpl(ld.render(design, label), p.shift_x, p.shift_y)
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
        design = self.active_design()
        if design.is_empty():
            messagebox.showinfo(APP_NAME, "Type something on the label first.", parent=self)
            return
        rendered = ld.render(design, self.current_label())
        if rendered.errors:
            messagebox.showerror(APP_NAME, "Fix these before printing:\n\n" + "\n".join(rendered.errors), parent=self)
            return
        try:
            copies = int(self.copies_var.get())
            if not 1 <= copies <= MAX_COPIES:
                raise ValueError
        except ValueError:
            messagebox.showerror(APP_NAME, f"Copies must be a whole number from 1 to {MAX_COPIES}.", parent=self)
            return
        if rendered.warnings and not messagebox.askyesno(
                APP_NAME, "\n".join(rendered.warnings) + "\n\nPrint anyway?", parent=self):
            return
        if copies > CONFIRM_COPIES_OVER and not messagebox.askyesno(
                APP_NAME, f"Print {copies} labels on {p.name}?", parent=self):
            return

        zpl = ld.build_zpl(rendered, p.shift_x, p.shift_y, copies)
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
            self._remember(design)
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

        def row(r, label, widget):
            ttk.Label(form, text=label).grid(row=r, column=0, sticky="w", pady=3)
            widget.grid(row=r, column=1, sticky="w", pady=3)
            self.form_widgets.append(widget)

        row(0, "Name", ttk.Entry(form, textvariable=self.vars["name"], width=30))
        self.windows_box = ttk.Combobox(form, textvariable=self.vars["windows_name"], state="readonly", width=40)
        self.windows_box.bind("<<ComboboxSelected>>", lambda e: self._windows_printer_picked())
        row(1, "Windows printer", self.windows_box)
        row(2, "IP address", ttk.Entry(form, textvariable=self.vars["host"], width=18))
        row(3, "Port", ttk.Entry(form, textvariable=self.vars["port"], width=8))
        model = ttk.Combobox(form, textvariable=self.vars["model"], values=list(MODELS), state="readonly", width=10)
        model.bind("<<ComboboxSelected>>", lambda e: self._model_changed())
        row(4, "Model", model)
        row(5, "Label width (in)", ttk.Entry(form, textvariable=self.vars["width_in"], width=8))
        row(6, "Label height (in)", ttk.Entry(form, textvariable=self.vars["height_in"], width=8))
        row(7, "Resolution (dpi)", ttk.Combobox(form, textvariable=self.vars["dpi"],
                                                values=[str(d) for d in DPI_TO_DPMM], state="readonly", width=8))
        row(8, "Shift right (dots)", ttk.Entry(form, textvariable=self.vars["shift_x"], width=8))
        row(9, "Shift down (dots)", ttk.Entry(form, textvariable=self.vars["shift_y"], width=8))

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
        label = printer_label(p)
        zpl = ld.build_zpl(ld.render(ld.Design.from_text(text), label), p.shift_x, p.shift_y, 1,
                           border=(label.width, label.height))
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
