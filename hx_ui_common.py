"""The shared Tk layer every MapleJet screen builds on.

Colours, styles and fonts; Texts, which re-applies a window's fixed text in the new
language; the choice boxes; BagText and PictureView, which draw bag text and read-back
pictures on canvases so the language switch can never touch them; HxWindow, the base of
every MapleJet window; tell/ask, the only dialogs; the worker-thread helpers; roles;
the per-user settings; and the alert sound's bytes.

Only the Tk thread touches widgets. Workers hand results back through the event bus or
services.bg.run(work, done); tests set STRICT_THREADS so a widget update from a worker
fails loudly instead of deadlocking Tk now and then.
"""

from __future__ import annotations

import base64
import io
import math
import re
import struct
import threading
import time
import traceback
import wave
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

import tkinter as tk
import tkinter.font
from tkinter import messagebox, ttk

from PIL import Image, ImageOps

if TYPE_CHECKING:
    from PIL import ImageTk

import hx_share
import hx_state
import hx_win
from hx_core import Msg
from hx_model import AMBER, BLUE, GREEN, GREY, RED, HxError
from i18n import N_, add_spanish, tr

# Texts used by two or more UI modules live here only (ui.md §16.1)
ES = {
    "Stripes hit a problem: {error}": "Stripes tuvo un problema: {error}",
    "{first} and {last}": "{first} y {last}",
    "(origin unknown)": "(origen desconocido)",
    "Save": "Guardar",
    "Revert": "Deshacer cambios",
    "Go back": "Volver",
    "Retry": "Reintentar",
    "Acknowledge": "Reconocer",
    "Stop printing": "Detener la impresión",
    "Stop printing...": "Detener la impresión...",
    "Resume printing": "Reanudar la impresión",
    "Send...": "Enviar...",
    "Set clocks...": "Ajustar relojes...",
    "Fix and send again": "Corregir y enviar de nuevo",
    "Try again": "Intentar de nuevo",
    "Leave it": "Dejarlo así",
    "Only Stripes MapleJet Senders can do this.":
        "Solo los miembros de Stripes MapleJet Senders pueden hacer esto.",
    "Only Stripes Administrators can do this.":
        "Solo los miembros de Stripes Administrators pueden hacer esto.",
    "Front photo": "Foto del frente",
    "Back photo": "Foto de atrás",
    "Artwork": "Arte",
    "Spec sheet": "Hoja de especificaciones",
    "Other file": "Otro archivo",
    "No photo": "Sin foto",
    "Code on the back of the bag": "Código en la parte de atrás de la bolsa",
    "Code on the front of the bag": "Código en el frente de la bolsa",
    "Encoder": "Encoder",
    "Delay": "Retardo",
    "Paper (Boxtron)": "Papel (Boxtron)",
    "Plastic": "Plástico",
    "White": "Blanca",
    "Black": "Negra",
    "In development": "En desarrollo",
    "Active": "Activo",
    "Not active": "Inactivo",
    "Line": "Línea",
    "Machine": "Máquina",
    "Machines": "Máquinas",
    "Product": "Producto",
    "Film": "Película",
    "Print text": "Texto de impresión",
    "Print texts": "Textos de impresión",
    "Lot": "Lote",
    "Floor board": "Tablero de planta",
    "No lines are set up yet.": "Todavía no hay líneas configuradas.",
    "Stop it at the printer now": "Deténgala en la impresora ahora",
    "Stripes couldn't stop printing on {machines}.": "Stripes no pudo detener la impresión en {machines}.",
    "Press Print On/Off on that printer now. Its card shows \"Stop it at the printer\" until Stripes reads "
    "that printing is off.":
        "Presione Print On/Off en esa impresora ahora. Su tarjeta muestra \"Deténgala en la impresora\" "
        "hasta que Stripes lea que la impresión está apagada.",
    "You can refresh once every {seconds} s.": "Puede actualizar una vez cada {seconds} s.",
}
add_spanish(ES)

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

# The Zebra monitor's colours, one per state colour
COLOUR_HEX = {GREEN: "#15803d", AMBER: "#b45309", RED: "#b91c1c", GREY: "#6b7280", BLUE: "#2563eb"}
PAGE_BG, CARD_BG, TEXT_FG, MUTED_FG, RULE_FG = "#f3f4f6", "#ffffff", "#111827", "#6b7280", "#d1d5db"
DIM_FG = "#9ca3af"                  # NOT MONITORED text on the floor board
HIGHLIGHT_BG = "#fde68a"            # an origin change in the Send and result windows
RED_BG, RED_FG = "#b91c1c", "#ffffff"   # the red windows (= COLOUR_HEX[RED])
UI_FONT = "Segoe UI"                # Tk substitutes its default family off Windows
BAG_FONTS = ("Segoe UI", "Arial", "Liberation Sans", "DejaVu Sans")   # BagText: the first installed
MONO_FONTS = ("Consolas", "Courier New", "Liberation Mono", "DejaVu Sans Mono")
APP_TITLE = "Stripes"               # title of tell/ask dialogs (a name, not translated)
STRICT_THREADS = False              # tests set True (ui.md §2.1 rule 7)
ALERT_WAV = "assets/alert.wav"
PREVIEW_MAX_SIDE = 480              # film photo previews (storage.md §18)

_PREVIEW_SIDES = (PREVIEW_MAX_SIDE, 360, 240)   # tried in turn until the PNG is small enough
_EMPTY_FILL = "#e5e7eb"             # PictureView with no picture
_PICTURE_BORDER = "#9ca3af"
_SIZE_ONLY = re.compile(r"\s*(\d+)x(\d+)\s*")    # a geometry with no position
_ALERT_RATE = 22050
_ALERT_FRAMES = 66150               # 3.000 s, so SND_LOOP repeats it every 3 s
_ALERT_TONES = ((880.0, 0.0, 0.35), (660.0, 0.40, 0.80))   # (Hz, from s, to s): higher, then lower
_ALERT_AMPLITUDE = 0.45
_ALERT_RAMP_S = 0.01                # fade in and out, so the tones don't click
_SHOW_TOPMOST_MS = 200
_ASK_WRAP_PX = 420


# --------------------------------------------------------------------------
# Threads
# --------------------------------------------------------------------------

def on_tk_thread():
    return threading.current_thread() is threading.main_thread()


def check_tk_thread():
    """In tests (STRICT_THREADS), a widget update from a worker thread raises at once."""
    if STRICT_THREADS and not on_tk_thread():
        raise RuntimeError("Tk call off the Tk thread")


class SyncBackground:
    """Tests: run(work, done) does the work and calls done at once, on the calling thread,
    the way stripes.Background does it in the app with a worker in between."""

    def run(self, work, done):
        try:
            result, error = work(), None
        except Exception as e:  # handed to done, like Background
            result, error = None, e
        done(result, error)


@dataclass(kw_only=True)
class WorkFailed:
    """Posted when a start_thread target raises."""
    ref: str                        # the send_id, action_id or run_id
    msg: Msg


def start_thread(services, name: str, ref: str, target: Callable[[], object]) -> threading.Thread:
    """Run a long job (a Send, an action, a test) on a named daemon thread. Its results arrive as
    bus events; if it raises, WorkFailed(ref=ref) is posted instead (ui.md §2.1 rule 4)."""
    bus = services.bus

    def run():
        try:
            target()
        except HxError as e:
            bus.post(WorkFailed(ref=ref, msg=e.msg))
        except Exception as e:
            traceback.print_exc()
            bus.post(WorkFailed(ref=ref, msg=Msg(N_("Stripes hit a problem: {error}"),
                                                 {"error": f"{type(e).__name__}: {e}"})))

    thread = threading.Thread(target=run, name=name, daemon=True)
    thread.start()
    return thread


# What is running that can_quit must ask about: key -> Msg, in the order started
_running: dict[str, Msg] = {}
_running_lock = threading.Lock()


def mark_running(key: str, what: Msg) -> None:
    """A Send, test or action that quitting now could leave halfway."""
    with _running_lock:
        _running[key] = what


def mark_done(key: str) -> None:
    with _running_lock:
        _running.pop(key, None)


def running() -> list[Msg]:
    with _running_lock:
        return list(_running.values())


# --------------------------------------------------------------------------
# Text
# --------------------------------------------------------------------------

def _tr(english, values):
    """tr() with Msg values translated first; a badly made text never stops the UI."""
    values = {k: text(v) if isinstance(v, Msg) else v for k, v in (values or {}).items()}
    try:
        return tr(english, **values)
    except (KeyError, IndexError, ValueError, AttributeError):
        return tr(english)


def text(msg: Msg | None) -> str:
    """A Msg in the current language, nested Msgs too; "" for None."""
    if msg is None:
        return ""
    return _tr(msg.text, msg.values)


def when(epoch: float | None) -> str:
    """A time as shown everywhere in the MapleJet screens (never translated)."""
    if epoch is None:
        return ""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(epoch))


def join_names(names: list[str]) -> str:
    """"A", "A and B", "A, B and C"."""
    names = list(names)
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    return tr("{first} and {last}", first=", ".join(names[:-1]), last=names[-1])


# --------------------------------------------------------------------------
# Look
# --------------------------------------------------------------------------

def setup_styles(root: tk.Misc) -> None:
    """The MapleJet ttk styles (ui.md §2.4). Safe to call again."""
    style = ttk.Style(root)
    style.configure("Hx.Main.TButton", font=(UI_FONT, 11, "bold"), padding=(16, 6))
    style.configure("Hx.Big.TButton", font=(UI_FONT, 14, "bold"), padding=(24, 10))
    style.configure("Hx.Head.TLabel", font=(UI_FONT, 15, "bold"))
    style.configure("Hx.Section.TLabel", font=(UI_FONT, 11, "bold"))
    style.configure("Hx.Small.TLabel", font=(UI_FONT, 8), foreground=MUTED_FG)
    style.configure("Hx.Card.TFrame", background=CARD_BG)
    style.configure("Hx.Card.TLabel", background=CARD_BG)
    # stripes.App defines these; set them only where it hasn't (tests, the self-check),
    # with its own values, so they are never redefined
    for name, colour in (("Warn.TLabel", COLOUR_HEX[AMBER]), ("Ok.TLabel", COLOUR_HEX[GREEN]),
                         ("Bad.TLabel", COLOUR_HEX[RED]), ("Muted.TLabel", MUTED_FG)):
        if not style.lookup(name, "foreground"):
            style.configure(name, foreground=colour)


_family_cache: dict[tuple[int, str], str] = {}


def _first_family(root, wanted, fallback):
    key = (id(root.tk), wanted[0])
    if key not in _family_cache:
        installed = set(tkinter.font.families(root))
        _family_cache[key] = next((f for f in wanted if f in installed), fallback)
    return _family_cache[key]


def bag_font_family(root: tk.Misc) -> str:
    return _first_family(root, BAG_FONTS, "TkDefaultFont")


def mono_font_family(root: tk.Misc) -> str:
    return _first_family(root, MONO_FONTS, "TkFixedFont")


# --------------------------------------------------------------------------
# Texts and choice boxes
# --------------------------------------------------------------------------

class Texts:
    """The fixed text of one window or frame: set once, re-applied in the new language by apply()."""

    def __init__(self):
        # (widget path, what) -> (widget, how to apply it, english, values)
        self._entries = {}

    def _remember(self, widget, what, apply, english, values):
        check_tk_thread()
        apply(widget, english, values)
        self._entries[(str(widget), what)] = (widget, apply, english, values)

    def set(self, widget, text: str, *, option: str = "text", **values) -> None:
        self._remember(widget, option,
                       lambda w, t, v: w.configure(**{option: _tr(t, v)}), text, values)

    def msg(self, widget, msg: Msg | None, *, option: str = "text") -> None:
        self._remember(widget, option,
                       lambda w, m, v: w.configure(**{option: text(m)}), msg, None)

    def title(self, window: tk.Wm, text: str, **values) -> None:
        self._remember(window, "title", lambda w, t, v: w.title(_tr(t, v)), text, values)

    def tab(self, notebook: ttk.Notebook, tab_id, text: str, **values) -> None:
        self._remember(notebook, ("tab", str(tab_id)),
                       lambda w, t, v: w.tab(tab_id, text=_tr(t, v)), text, values)

    def heading(self, tree: ttk.Treeview, column: str, text: str, **values) -> None:
        self._remember(tree, ("heading", column),
                       lambda w, t, v: w.heading(column, text=_tr(t, v)), text, values)

    def forget(self, widget) -> None:
        """Drop every entry of that widget."""
        path = str(widget)
        for key in [k for k in self._entries if k[0] == path]:
            del self._entries[key]

    def apply(self) -> None:
        """Show every remembered text again in the current language; entries of widgets that are
        gone are dropped."""
        check_tk_thread()
        for key, (widget, apply, english, values) in list(self._entries.items()):
            try:
                if widget.winfo_exists():
                    apply(widget, english, values)
                    continue
            except tk.TclError:
                pass
            self._entries.pop(key, None)


class ChoiceBox(ttk.Combobox):
    """Read-only combobox of fixed choices, shown translated; the program sees keys.

    A choice is (key, N_ text), or (key, Msg) when its text has values (the screens of
    ui.md §14: "{n}: {width} x {height} (main)"), so retranslate() can show it again."""

    def __init__(self, master, choices: list[tuple[str, str | Msg]], **kw):
        kw.setdefault("state", "readonly")
        super().__init__(master, **kw)
        self._choices = []
        self.set_choices(choices)

    def key(self) -> str:
        """The chosen key; "" when nothing is chosen."""
        index = self.current()
        if 0 <= index < len(self._choices):
            return self._choices[index][0]
        return ""

    def set_key(self, key: str) -> None:
        keys = [k for k, _ in self._choices]
        if key in keys:
            self.current(keys.index(key))
        else:
            self.set("")

    def set_choices(self, choices: list[tuple[str, str | Msg]]) -> None:
        """New choices; the chosen key stays chosen when it is still one of them."""
        key = self.key() if self._choices else ""
        self._choices = [(str(k), t) for k, t in choices]
        self.configure(values=[text(t) if isinstance(t, Msg) else tr(t) for _, t in self._choices])
        self.set_key(key)

    def retranslate(self) -> None:
        """Show the values in the current language, keeping the chosen key."""
        self.set_choices(self._choices)


class RecordBox(ttk.Combobox):
    """Read-only combobox of records, shown by name (names are data: never translated)."""

    def __init__(self, master, **kw):
        kw.setdefault("state", "readonly")
        super().__init__(master, **kw)
        self._ids = []
        self._names = []
        self._none_text = ""

    def set_records(self, records: list[tuple[str, str]], *, none_text: str = "") -> None:
        """(id, display name) pairs; none_text (an N_ text) adds a first entry meaning "" (for
        example "All machines"). The chosen record stays chosen when it is still listed."""
        chosen = self.record_id()
        had_choice = self.current() >= 0
        self._none_text = none_text
        self._ids = ([""] if none_text else []) + [str(i) for i, _ in records]
        self._names = [name for _, name in records]
        self._show()
        if had_choice:
            self.set_record_id(chosen)
        else:
            self.set("")

    def record_id(self) -> str:
        index = self.current()
        if 0 <= index < len(self._ids):
            return self._ids[index]
        return ""

    def set_record_id(self, record_id: str) -> None:
        if record_id in self._ids:
            self.current(self._ids.index(record_id))
        else:
            self.set("")

    def retranslate(self) -> None:
        """Show none_text in the current language, keeping the chosen record."""
        index = self.current()
        self._show()
        if index >= 0:
            self.current(index)

    def _show(self):
        self.configure(values=([tr(self._none_text)] if self._none_text else []) + self._names)


def retranslate_boxes(widget) -> None:
    """retranslate() every ChoiceBox and RecordBox inside widget, but not inside another
    HxWindow (each window does its own)."""
    for child in widget.winfo_children():
        if isinstance(child, HxWindow):
            continue
        if isinstance(child, (ChoiceBox, RecordBox)):
            child.retranslate()
        retranslate_boxes(child)


# --------------------------------------------------------------------------
# Bag text and pictures
# --------------------------------------------------------------------------

class BagText(tk.Canvas):
    """Bag text: drawn on a canvas, so the language switch can't touch it. English only,
    shown exactly as given (no strip, no case change)."""
    hx_no_translate = True

    def __init__(self, master, *, size: int = 12, bold: bool = False, wrap_px: int | None = None, **kw):
        foreground = kw.pop("foreground", TEXT_FG)
        background = kw.pop("background", CARD_BG)
        family = kw.pop("family", None) or bag_font_family(master)
        kw.setdefault("highlightthickness", 0)
        kw.setdefault("borderwidth", 0)
        if wrap_px:
            kw.setdefault("width", wrap_px + 4)
        super().__init__(master, background=background, **kw)
        self.wrap_px = wrap_px
        self._text = ""
        self.text_item = self.create_text(2, 2, anchor="nw", text="", fill=foreground,
                                          font=(family, size, "bold" if bold else "normal"),
                                          width=wrap_px or 0)
        self._fit()

    def set(self, text: str) -> None:
        check_tk_thread()
        self._text = "" if text is None else text
        self.itemconfigure(self.text_item, text=self._text)
        self._fit()

    def get(self) -> str:
        """Exactly what was last set ("" before)."""
        return self._text

    def set_colours(self, *, foreground: str | None = None, background: str | None = None) -> None:
        check_tk_thread()
        if foreground is not None:
            self.itemconfigure(self.text_item, fill=foreground)
        if background is not None:
            self.configure(background=background)

    def _fit(self):
        # An empty text item still measures one line high, so the layout doesn't jump
        x1, y1, x2, y2 = self.bbox(self.text_item) or (0, 0, 0, 0)
        self.configure(height=y2 - y1 + 4)
        if self.wrap_px is None:
            self.configure(width=x2 - x1 + 4)


def _photo_image(image, master):
    """ImageTk.PhotoImage(image, master=master). Some Linux Pillow builds leave ImageTk out
    (stripes.py imports it lazily for the same reason); Tk then reads the picture as PNG itself."""
    try:
        from PIL import ImageTk
    except ImportError:
        out = io.BytesIO()
        image.save(out, "PNG")
        return tk.PhotoImage(master=master, format="png", data=base64.b64encode(out.getvalue()).decode("ascii"))
    return ImageTk.PhotoImage(image, master=master)


def bmp_to_image(bmp: bytes) -> "Image.Image":
    """A read-back BMP (or any picture Pillow reads) as a Pillow image, fully loaded."""
    image = Image.open(io.BytesIO(bmp))
    image.load()
    return image


class PictureView(tk.Canvas):
    """A read-back picture (1024 x 160), scaled to fit; a grey box when there is none."""
    hx_no_translate = True

    def __init__(self, master, *, width: int = 512, **kw):
        kw.setdefault("highlightthickness", 0)
        kw.setdefault("borderwidth", 0)
        kw.setdefault("background", CARD_BG)
        kw.setdefault("height", round(width * 160 / 1024) + 2)
        super().__init__(master, width=width, **kw)
        self.bmp = None
        self._image = None          # the picture as a grey-scale Pillow image
        self._failed = False        # Pillow couldn't read the last BMP
        self._photo = None          # the PhotoImage on show (Tk drops it if nothing keeps it)
        self._drawn_size = None
        self.bind("<Configure>", self._on_configure, add="+")
        self._draw()

    def set_bmp(self, bmp: bytes | None) -> None:
        """Show a picture; never raises for a missing or unreadable one."""
        check_tk_thread()
        self.bmp = bmp
        self._image = None
        self._failed = False
        if bmp:
            try:
                self._image = bmp_to_image(bmp).convert("L")
            except Exception:
                self._failed = True
        self._draw()

    def _size(self):
        width, height = self.winfo_width(), self.winfo_height()
        if width <= 1 or height <= 1:    # not laid out yet
            width, height = self.winfo_reqwidth(), self.winfo_reqheight()
        return max(width, 4), max(height, 4)

    def _on_configure(self, event):
        if self._size() != self._drawn_size:
            self._draw()

    def _draw(self):
        width, height = self._size()
        self._drawn_size = (width, height)
        self.delete("all")
        self._photo = None
        if self._image is None:
            outline = COLOUR_HEX[RED] if self._failed else RULE_FG
            self.create_rectangle(0, 0, width - 1, height - 1, fill=_EMPTY_FILL, outline=outline)
            return
        # Fit inside the canvas, keeping 1024:160, with a 1 px border around it
        source_w, source_h = self._image.size
        w = max(1, width - 2)
        h = max(1, round(w * source_h / source_w))
        if h > height - 2:
            h = max(1, height - 2)
            w = max(1, round(h * source_w / source_h))
        image = self._image
        if (w, h) != image.size:
            resample = Image.LANCZOS if w < source_w else Image.NEAREST
            image = image.resize((w, h), resample)
        self._photo = _photo_image(image, self)
        self.create_image(1, 1, anchor="nw", image=self._photo)
        self.create_rectangle(0, 0, w + 1, h + 1, outline=_PICTURE_BORDER)


def png_preview(path: str) -> bytes | None:
    """Worker thread: a PNG preview of a picture file (longest side 480 px, then 360 or 240 if it
    is still too big for the share); None when Pillow can't open the file or it won't fit."""
    try:
        with Image.open(path) as opened:
            opened.draft("RGB", (PREVIEW_MAX_SIDE, PREVIEW_MAX_SIDE))   # JPEG: decode at a smaller scale
            picture = ImageOps.exif_transpose(opened).convert("RGB")
        for side in _PREVIEW_SIDES:
            thumb = picture.copy()
            thumb.thumbnail((side, side), Image.LANCZOS)
            out = io.BytesIO()
            thumb.save(out, "PNG", optimize=True)
            if len(out.getvalue()) <= hx_share.FILM_PREVIEW_MAX_BYTES:
                return out.getvalue()
    except Exception:   # UnidentifiedImageError, OSError, DecompressionBombError, ...
        return None
    return None


def photo_from_png(master, png: bytes, *, height: int | None = None) -> "ImageTk.PhotoImage":
    """A PhotoImage of PNG bytes already in memory (Tk thread), scaled to height when given.
    The caller keeps a reference to it. Raises for bytes Pillow can't read."""
    image = Image.open(io.BytesIO(png))
    image.load()
    if height and image.height and image.height != height:
        width = max(1, round(image.width * height / image.height))
        resample = Image.LANCZOS if height < image.height else Image.NEAREST
        image = image.resize((width, height), resample)
    return _photo_image(image, master)


def alert_wav_bytes() -> bytes:
    """The exact bytes of assets/alert.wav (ui.md §9.4): 3.0 s, a two-tone chime of 0.8 s,
    then silence; Windows loops it, so it rings every 3 s."""
    frames = bytearray()
    for i in range(_ALERT_FRAMES):
        t = i / _ALERT_RATE
        sample = 0
        for freq, t0, t1 in _ALERT_TONES:
            if t0 <= t < t1:
                env = min(1, (t - t0) / _ALERT_RAMP_S, (t1 - t) / _ALERT_RAMP_S)
                sample = round(_ALERT_AMPLITUDE * 32767 * env * math.sin(2 * math.pi * freq * (t - t0)))
        frames += struct.pack("<h", sample)
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(_ALERT_RATE)
        w.writeframes(bytes(frames))
    return out.getvalue()


# --------------------------------------------------------------------------
# Scrolling
# --------------------------------------------------------------------------

# Widgets that scroll themselves on the mouse wheel: the Scroller leaves them alone
_OWN_WHEEL = ("Text", "Listbox", "Treeview", "TScrollbar", "Scrollbar")
# Widgets whose class binding changes their value on the mouse wheel. Inside a Scroller the
# wheel scrolls the page instead, so scrolling never silently changes a choice or a number
_VALUE_WHEEL = ("TCombobox", "TSpinbox", "Spinbox", "TScale", "Scale")
_WHEEL_SEQUENCES = ("MouseWheel>", "Button-4>", "Button-5>")


class Scroller(ttk.Frame):
    """A vertically scrolling area: put children in .inner. Mouse wheel scrolls it while the pointer is over it."""

    def __init__(self, master, *, background: str = PAGE_BG, **kw):
        super().__init__(master, **kw)
        self.canvas = tk.Canvas(self, background=background, highlightthickness=0, borderwidth=0)
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.canvas, background=background)
        self._window = self.canvas.create_window(0, 0, window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", lambda e: self._update_region(), add="+")
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self._window, width=e.width),
                         add="+")
        _bind_wheel(self)

    def _update_region(self):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def can_scroll(self):
        first, last = self.canvas.yview()
        return first > 0.0 or last < 1.0

    def scroll(self, units):
        self.canvas.yview_scroll(units, "units")


def _wheel_target(widget):
    """The Scroller the wheel should move for a pointer over widget, or None."""
    while widget is not None:
        try:
            if widget.winfo_class() in _OWN_WHEEL:
                return None
        except tk.TclError:
            return None
        if isinstance(widget, Scroller) and widget.can_scroll():
            return widget
        if isinstance(widget, (tk.Tk, tk.Toplevel)):
            return None
        widget = widget.master
    return None


def _in_scroller(widget):
    """True when widget is inside a Scroller of its window."""
    while isinstance(widget, tk.Misc) and not isinstance(widget, (tk.Tk, tk.Toplevel)):
        if isinstance(widget, Scroller):
            return True
        widget = widget.master
    return False


def _on_wheel(event):
    try:
        root = event.widget._root() if hasattr(event.widget, "_root") else None
        widget = root.winfo_containing(event.x_root, event.y_root) if root else None
    except (KeyError, tk.TclError):
        return
    target = _wheel_target(widget)
    if target is None:
        return
    if event.num == 4:
        units = -1
    elif event.num == 5:
        units = 1
    else:
        units = -int(event.delta / 120) or (-1 if event.delta > 0 else 1)
    target.scroll(units)


def _on_value_wheel(event):
    """Runs before the wheel class binding of a combobox, spinbox or scale. Inside a Scroller it
    scrolls the page and stops the class binding, so the value stays as it is; elsewhere it
    does nothing and the class binding runs as usual."""
    if not _in_scroller(event.widget):
        return None
    _on_wheel(event)
    return "break"


def _bind_wheel(widget):
    # One "all" binding per Tk, which finds the Scroller under the pointer: nested and
    # side-by-side Scrollers work, and no other binding is replaced
    root = widget._root()
    if getattr(root, "_hx_wheel_bound", False):
        return
    root._hx_wheel_bound = True
    for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
        root.bind_all(sequence, _on_wheel, add="+")
    # The "all" binding runs after the class binding, too late to keep a combobox from
    # stepping its value, so _on_value_wheel is put in front of those class bindings. Their
    # own script is kept after it and still runs outside a Scroller (the Zebra tab)
    for cls in _VALUE_WHEEL:
        for sequence in root.bind_class(cls):
            if not sequence.endswith(_WHEEL_SEQUENCES):
                continue
            own = root.bind_class(cls, sequence)
            root.bind_class(cls, sequence, _on_value_wheel)
            root.bind_class(cls, sequence, root.bind_class(cls, sequence) + own)


# --------------------------------------------------------------------------
# Windows
# --------------------------------------------------------------------------

_open: list["HxWindow"] = []    # every HxWindow not yet destroyed, in opening order


def _primary_screen(widget):
    """(x, y, width, height) of the main screen."""
    try:
        for m in hx_win.monitors():
            if m.primary:
                return m.x, m.y, m.width, m.height
    except Exception:   # monitors() is a convenience here; Tk's screen will do
        pass
    return 0, 0, widget.winfo_screenwidth(), widget.winfo_screenheight()


def _centred(window, width, height):
    """(x, y) placing a window of that size centred on the main window when it is visible,
    else on the main screen (ui.md §2.5 rule 4)."""
    master = window.master
    try:
        main = master.winfo_toplevel() if master is not None else None
        if main is not None and main is not window and main.winfo_viewable():
            mx, my = main.winfo_rootx(), main.winfo_rooty()
            x = mx + (main.winfo_width() - width) // 2
            y = my + (main.winfo_height() - height) // 2
            return (max(0, x) if mx >= 0 else x), (max(0, y) if my >= 0 else y)
    except tk.TclError:
        pass
    sx, sy, sw, sh = _primary_screen(window)
    return sx + max(0, (sw - width) // 2), sy + max(0, (sh - height) // 2)


class HxWindow(tk.Toplevel):
    """The base of every MapleJet window.

    A window with a key (a class attribute, or self.key set before calling
    HxWindow.__init__) is registered in services.nav.windows while it is open, so the
    Navigator keeps one window per key. geometry("WxH") with no position opens the window
    centred; a position given is kept as it is.
    """
    hx_window = True                # the Zebra shortcuts ignore these windows
    hx_no_translate = True          # App._retranslate skips it; retranslate() does the work
    key: str = ""                   # Navigator keeps one open window per key

    def __init__(self, master, services=None, *, title: str = "", **kw):
        self._hx_tokens = []
        self._hx_timers = set()
        self._hx_destroyed = False
        self._hx_show_timer = None
        self._hx_topmost = False        # the window's own -topmost (the red windows set it for good)
        super().__init__(master, **kw)
        self.services = services
        self.texts = Texts()
        setup_styles(self)
        if title:
            self.texts.title(self, title)
        self.protocol("WM_DELETE_WINDOW", self.close)
        _open.append(self)
        windows = self._nav_windows()
        if self.key and windows is not None:
            windows[self.key] = self
        self.bind("<Destroy>", self._on_destroy_event, add="+")

    def _nav_windows(self):
        windows = getattr(getattr(self.services, "nav", None), "windows", None)
        return windows if isinstance(windows, dict) else None

    def _unregister(self):
        if self in _open:
            _open.remove(self)
        windows = self._nav_windows()
        if windows is not None:
            for key in [k for k, w in windows.items() if w is self]:
                del windows[key]

    def _on_destroy_event(self, event):
        # Also when Tk destroys the window without destroy() (its master went first)
        if event.widget is self:
            self._cleanup()

    def _cleanup(self):
        """Unsubscribe, cancel the timers and leave the open windows; once."""
        if self._hx_destroyed:
            return
        self._hx_destroyed = True
        bus = getattr(self.services, "bus", None)
        for token in self._hx_tokens:
            bus.unsubscribe(token)
        self._hx_tokens.clear()
        for timer in list(self._hx_timers):
            try:
                self.after_cancel(timer)
            except tk.TclError:
                pass
        self._hx_timers.clear()
        self._unregister()

    # events and timers

    def subscribe(self, event_type: type, callback) -> None:
        """bus.subscribe for as long as the window is open; callbacks run on the Tk thread."""
        if self.services is None:
            raise RuntimeError("HxWindow.subscribe needs services")

        def deliver(event):
            check_tk_thread()
            if not self._hx_destroyed:
                callback(event)

        self._hx_tokens.append(self.services.bus.subscribe(event_type, deliver))

    def later(self, ms: int, func) -> str:
        """self.after(), cancelled when the window is destroyed."""
        timer = None

        def run():
            self._hx_timers.discard(timer)
            if not self._hx_destroyed:
                func()

        timer = self.after(ms, run)
        self._hx_timers.add(timer)
        return timer

    # language

    def retranslate(self) -> None:
        """The window's text in the current language; bag text is never touched."""
        self.texts.apply()
        retranslate_boxes(self)
        self.refresh_texts()

    def refresh_texts(self) -> None:
        """Subclasses re-render their Msg-based text (headlines, problems, progress) here."""

    # showing and closing

    def wm_geometry(self, newGeometry=None):
        if isinstance(newGeometry, str):
            size = _SIZE_ONLY.fullmatch(newGeometry)
            if size:
                width, height = int(size.group(1)), int(size.group(2))
                x, y = _centred(self, width, height)
                newGeometry = f"{width}x{height}+{x}+{y}"
        return super().wm_geometry(newGeometry)

    geometry = wm_geometry

    def wm_attributes(self, *args, **kwargs):
        # Remember -topmost as the window sets it: a window manager may report it late or,
        # with none (Linux tests), never, and show() must not undo a window's own setting
        for name, value in list(zip(args[::2], args[1::2])) + list(kwargs.items()):
            if str(name).lstrip("-") == "topmost":
                self._hx_topmost = self.tk.getboolean(value) if isinstance(value, str) else bool(value)
        return super().wm_attributes(*args, **kwargs)

    attributes = wm_attributes

    def close(self) -> None:
        """The close box calls this. Windows that are running something override it."""
        self.destroy()

    def show(self) -> None:
        """Bring the window to the front: raised and topmost for 200 ms, then an ordinary window
        again (a window that is topmost for good stays so)."""
        self.deiconify()
        self.lift()
        if self._hx_show_timer is None and not self._hx_topmost:
            self._raise_topmost(True)
            self._hx_show_timer = self.later(_SHOW_TOPMOST_MS, self._end_show)
        try:
            self.focus_force()
        except tk.TclError:
            pass

    def _end_show(self):
        self._hx_show_timer = None
        if not self._hx_topmost:
            self._raise_topmost(False)

    def _raise_topmost(self, on):
        # Straight to Tk: show()'s 200 ms isn't the window's own setting
        tk.Wm.wm_attributes(self, "-topmost", on)

    def destroy(self) -> None:
        self._cleanup()
        super().destroy()


def open_windows() -> list[HxWindow]:
    """Every live HxWindow, in opening order."""
    alive = []
    for window in list(_open):
        try:
            if window.winfo_exists():
                alive.append(window)
                continue
        except tk.TclError:
            pass
        _open.remove(window)
    return alive


class PhotoWindow(HxWindow):
    """A film photo, larger (Send window and film editor). The caption is data, not translated."""

    def __init__(self, master, services, *, png: bytes, caption: str):
        super().__init__(master, services)
        self.title(caption or APP_TITLE)
        self.geometry("560x560")
        self.photo = None
        body = ttk.Frame(self, padding=12)
        body.pack(fill="both", expand=True)
        self.caption_label = ttk.Label(body, text=caption, style="Hx.Section.TLabel")
        self.caption_label.pack(anchor="w", pady=(0, 8))
        self.picture_label = ttk.Label(body, anchor="center")
        self.picture_label.pack(fill="both", expand=True)
        try:
            self.photo = photo_from_png(self, png)
            self.picture_label.configure(image=self.photo)
        except Exception:
            self.picture_label.configure(style="Muted.TLabel")
            self.texts.set(self.picture_label, N_("No photo"))
        self.close_button = ttk.Button(body, command=self.close)
        self.texts.set(self.close_button, N_("Close"))
        self.close_button.pack(anchor="e", pady=(8, 0))
        self.bind("<Escape>", lambda e: self.close())


# --------------------------------------------------------------------------
# Dialogs
# --------------------------------------------------------------------------

def tell(parent, msg: Msg, *, kind: str = "info") -> None:
    """A message box; kind is "info", "warning" or "error". Tests patch this."""
    show = {"warning": messagebox.showwarning, "error": messagebox.showerror}.get(kind, messagebox.showinfo)
    show(APP_TITLE, text(msg), parent=parent)


class _AskDialog(tk.Toplevel):
    """The small modal question of ask()."""
    hx_window = True                # the Zebra shortcuts ignore it too
    hx_no_translate = True

    def __init__(self, parent, question: str, yes: str, no: str):
        super().__init__(parent)
        self.answer = False
        self.withdraw()
        self.title(APP_TITLE)
        self.resizable(False, False)
        setup_styles(self)
        body = ttk.Frame(self, padding=16)
        body.pack(fill="both", expand=True)
        self.question_label = ttk.Label(body, text=question, wraplength=_ASK_WRAP_PX, justify="left")
        self.question_label.pack(anchor="w", pady=(0, 16))
        buttons = ttk.Frame(body)
        buttons.pack(fill="x")
        self.yes_button = ttk.Button(buttons, text=yes, style="Hx.Main.TButton", command=self._yes)
        self.yes_button.pack(side="right")
        self.no_button = ttk.Button(buttons, text=no, command=self.destroy)
        self.no_button.pack(side="right", padx=(0, 8))
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.bind("<Escape>", lambda e: self.destroy())
        owner = parent.winfo_toplevel() if parent is not None else None
        try:
            if owner is not None and owner.winfo_viewable():
                self.transient(owner)
        except tk.TclError:
            pass
        self.update_idletasks()
        x, y = _centred(self, self.winfo_reqwidth(), self.winfo_reqheight())
        self.geometry(f"+{x}+{y}")
        self.deiconify()
        self.no_button.focus_set()

    def _yes(self):
        self.answer = True
        self.destroy()


def ask(parent, msg: Msg, *, yes: str, no: str) -> bool:
    """A yes/no question (yes and no are N_ texts); True for yes. Escape and the close box
    mean no. Tests patch this."""
    dialog = _AskDialog(parent, text(msg), tr(yes), tr(no))
    try:
        dialog.wait_visibility()
        dialog.grab_set()
    except tk.TclError:
        pass    # an unmodal question is better than none
    dialog.wait_window()
    return dialog.answer


# --------------------------------------------------------------------------
# Roles and settings
# --------------------------------------------------------------------------

@dataclass(frozen=True, kw_only=True)
class Roles:
    sender: bool = False
    admin: bool = False
    acknowledge: bool = False


def roles(services) -> Roles:
    """What the signed-in person may do, from the company's group SIDs (works offline);
    nothing while no data is loaded (ui.md §2.7)."""
    data = services.data.current
    if data is None:
        return Roles()
    company, identity = data.company, services.identity
    return Roles(sender=identity.is_sender(company), admin=identity.is_admin(company),
                 acknowledge=hx_state.can_acknowledge(identity, company))


@dataclass(kw_only=True)
class HxSettings:
    """This user's MapleJet settings: %APPDATA%\\Stripes\\v2\\maplejet.json (roams with them)."""
    SIZE_PATTERN = r"([0-9]{3,5})x([0-9]{3,5})"
    MIN_WINDOW = (640, 360)
    _FILE = "maplejet.json"

    floor_board_at_startup: bool = False
    floor_board_screen: int = 0         # index into hx_win.monitors(); 0 is the main screen
    floor_board_size: str = "full"      # "full" or "<width>x<height>"

    @staticmethod
    def load(local) -> "HxSettings":
        """Each field only when it has the right type and value; anything else gets its default."""
        try:
            d = local.read_user_json(HxSettings._FILE)
        except (OSError, ValueError):
            d = {}
        if not isinstance(d, dict):
            d = {}
        settings = HxSettings()
        at_startup = d.get("floor_board_at_startup")
        if type(at_startup) is bool:
            settings.floor_board_at_startup = at_startup
        screen = d.get("floor_board_screen")
        if type(screen) is int and screen >= 0:
            settings.floor_board_screen = screen
        size = d.get("floor_board_size")
        if size == "full" or HxSettings._window_size(size) is not None:
            settings.floor_board_size = size
        return settings

    def save(self, local) -> None:
        """Worker thread (bg.run)."""
        local.write_user_json(self._FILE, {"floor_board_at_startup": self.floor_board_at_startup,
                                          "floor_board_screen": self.floor_board_screen,
                                          "floor_board_size": self.floor_board_size})

    def size_tuple(self) -> tuple[int, int] | None:
        """(width, height) of a floor board in a window; None for full screen (and for a size
        that isn't valid, which counts as full screen)."""
        return self._window_size(self.floor_board_size)

    @staticmethod
    def _window_size(size):
        if not isinstance(size, str):
            return None
        found = re.fullmatch(HxSettings.SIZE_PATTERN, size)
        if not found:
            return None
        width, height = int(found.group(1)), int(found.group(2))
        if width < HxSettings.MIN_WINDOW[0] or height < HxSettings.MIN_WINDOW[1]:
            return None
        return width, height
