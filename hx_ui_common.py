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
import os
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

    from hx_model import FileRef

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
    # PhotoWindow
    "Full size": "Tamaño real",
    "Fit to window": "Ajustar a la ventana",
    "Open the file": "Abrir el archivo",
    "Loading the original photo...": "Cargando la foto original...",
    "Showing the preview: Stripes couldn't read the original photo.":
        "Se muestra la vista previa: Stripes no pudo leer la foto original.",
    "Drag the photo to move around it.": "Arrastre la foto para moverse por ella.",
    "Stripes can't reach the server, so the original can't be opened now.":
        "Stripes no puede conectar con el servidor, así que el original no se puede abrir ahora.",
    "Opening files works on Windows only. The file is at {path}.":
        "Abrir archivos solo funciona en Windows. El archivo está en {path}.",
    "Stripes couldn't open the file: {error}": "Stripes no pudo abrir el archivo: {error}",
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
PHOTO_SCREEN_SHARE = 0.9            # PhotoWindow: at most this share of the screen's width and height

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
_PHOTO_FIT_MS = 100                 # PhotoWindow: fitted again this long after the last resize
_PHOTO_SCROLL_PX = 40               # PhotoWindow: one wheel step at full size


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
    """A time as shown everywhere in the MapleJet screens (never translated). Never raises: times
    come from other PCs' files, and one this PC can't show (inf, nan, out of range) is "?"."""
    if epoch is None:
        return ""
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(epoch))
    except (OverflowError, OSError, ValueError, TypeError):
        return "?"


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
    # with its own values, so they are never redefined. configure(name, option) gives the style's
    # own value ("" when unset); lookup() would inherit the theme's "." foreground and never be empty
    for name, colour in (("Warn.TLabel", COLOUR_HEX[AMBER]), ("Ok.TLabel", COLOUR_HEX[GREEN]),
                         ("Bad.TLabel", COLOUR_HEX[RED]), ("Muted.TLabel", MUTED_FG)):
        if not str(style.configure(name, "foreground") or ""):
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


def fit_size(size: tuple[int, int], box: tuple[int, int]) -> tuple[int, int]:
    """(width, height) of a picture of that size scaled to fit inside box, keeping its shape."""
    width, height = size
    scale = min(box[0] / width, box[1] / height)
    return max(1, round(width * scale)), max(1, round(height * scale))


def photo_from_png(master, png: bytes, *, height: int | None = None,
                   box: tuple[int, int] | None = None) -> "ImageTk.PhotoImage":
    """A PhotoImage of PNG bytes already in memory (Tk thread), scaled to height, or to fit inside
    box (width, height), when given; either way it keeps its shape. The caller keeps a reference
    to it. Raises for bytes Pillow can't read."""
    image = Image.open(io.BytesIO(png))
    image.load()
    size = image.size
    if box and image.width and image.height:
        size = fit_size(image.size, box)
    elif height and image.height:
        size = (max(1, round(image.width * height / image.height)), height)
    if size != image.size:
        resample = Image.LANCZOS if size[1] < image.height else Image.NEAREST
        image = image.resize(size, resample)
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
    """A film photo, large (Send window and film editor). The caption is data, not translated.

    It opens at once with the PNG preview, sized to the photo and at most 90% of the screen, with
    its buttons always in view. Given the photo's FileRef (original), it reads the original file
    from the share on a worker thread and shows it scaled to fit the window; "Full size" shows it
    pixel for pixel on a canvas that scrolls and drags, so the small print (a nutrition panel, a
    barcode) can be read, and "Open the file" opens it in the PC's own viewer. Until the original
    is in, or when it can't be read, the preview is shown."""

    def __init__(self, master, services, *, png: bytes, caption: str, original: "FileRef | None" = None):
        if original is not None:
            self.key = f"photo:{original.sha256}"       # one window per photo (show_photo)
        super().__init__(master, services)
        self.title(caption or APP_TITLE)
        self.original = original
        self.image = None           # the Pillow image on show: the preview, then the original
        self.source = "preview"     # "preview" | "original"
        self.full = False           # True: pixel for pixel, scrolling; False: fitted to the window
        self.photo = None           # the PhotoImage on show (Tk drops it if nothing keeps it)
        self.shown_size = None      # (width, height) of the picture as drawn
        self._screen_image = None   # the original shrunk to the screen: what fitting starts from
        self._full_photo = None     # (image, PhotoImage) of the last full-size view
        self._fitted_for = None     # the canvas size the fitted picture was drawn for
        self._fit_timer = None
        self._most = (1, 1)         # the largest the window may be
        self._around = (0, 0)       # what the window needs around the picture (caption, note, buttons)
        self._fit_geometry = ""     # the window's geometry before Full size, for Fit to window
        try:
            image = Image.open(io.BytesIO(png))
            image.load()
            self.image = image if image.mode in ("RGB", "L") else image.convert("RGB")
        except Exception:   # not a picture Pillow reads: "No photo"
            self.image = None
        self._build(caption)
        self._size_window()
        self._draw()
        self.bind("<Escape>", lambda e: self.close())
        if original is not None and services is not None:
            self._note(Msg(N_("Loading the original photo...")))
            self.later(0, self._load_original)

    def _build(self, caption):
        texts = self.texts
        body = ttk.Frame(self, padding=12)
        body.pack(fill="both", expand=True)
        self.caption_label = ttk.Label(body, text=caption, style="Hx.Section.TLabel")   # data
        self.caption_label.pack(side="top", anchor="w", pady=(0, 8))
        # The buttons and the note are packed before the picture, so a small window squeezes the
        # picture and never them
        bar = ttk.Frame(body)
        bar.pack(side="bottom", fill="x", pady=(8, 0))
        self.close_button = ttk.Button(bar, command=self.close)
        texts.set(self.close_button, N_("Close"))
        self.close_button.pack(side="right")
        self.zoom_button = ttk.Button(bar, command=self.toggle_zoom)
        texts.set(self.zoom_button, N_("Full size"))
        self.open_button = ttk.Button(bar, command=self.open_file)
        texts.set(self.open_button, N_("Open the file"))
        if self.original is not None:   # without one there is only the preview: nothing to zoom or open
            self.zoom_button.pack(side="left")
            self.zoom_button.state(["disabled"])     # until the original is in
            self.open_button.pack(side="left", padx=(8, 0))
        self.note_label = ttk.Label(body, style="Muted.TLabel", justify="left")
        self.note_label.pack(side="bottom", fill="x", pady=(6, 0))
        body.bind("<Configure>", lambda e: self.note_label.configure(wraplength=max(200, e.width - 24)), add="+")
        area = ttk.Frame(body)
        area.pack(side="top", fill="both", expand=True)
        area.rowconfigure(0, weight=1)
        area.columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(area, highlightthickness=0, borderwidth=0,
                                xscrollincrement=_PHOTO_SCROLL_PX, yscrollincrement=_PHOTO_SCROLL_PX)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.yscroll = ttk.Scrollbar(area, orient="vertical", command=self.canvas.yview)
        self.yscroll.grid(row=0, column=1, sticky="ns")
        self.xscroll = ttk.Scrollbar(area, orient="horizontal", command=self.canvas.xview)
        self.xscroll.grid(row=1, column=0, sticky="ew")
        self.canvas.configure(xscrollcommand=self.xscroll.set, yscrollcommand=self.yscroll.set)
        self.yscroll.grid_remove()      # only at full size
        self.xscroll.grid_remove()
        self.picture_label = ttk.Label(area, anchor="center", style="Muted.TLabel")
        self.picture_label.grid(row=0, column=0, sticky="nsew")
        texts.set(self.picture_label, N_("No photo"))
        if self.image is None:
            self.canvas.grid_remove()
        else:
            self.picture_label.grid_remove()
        self.canvas.bind("<Configure>", self._on_resize, add="+")
        self.canvas.bind("<Double-Button-1>", self._on_double, add="+")
        self.canvas.bind("<ButtonPress-1>", self._on_press, add="+")
        self.canvas.bind("<B1-Motion>", self._on_drag, add="+")
        # On the window, so the wheel works wherever Windows sends it (the focus, not the pointer)
        for sequence, axis, step in (("<MouseWheel>", "y", 0), ("<Shift-MouseWheel>", "x", 0),
                                     ("<Button-4>", "y", -1), ("<Button-5>", "y", 1),
                                     ("<Shift-Button-4>", "x", -1), ("<Shift-Button-5>", "x", 1)):
            self.bind(sequence, lambda e, a=axis, s=step: self._wheel(e, a, s), add="+")

    def _note(self, msg: Msg | None, style: str = "Muted.TLabel") -> None:
        self.texts.msg(self.note_label, msg)
        self.note_label.configure(style=style)

    def _gone(self):
        try:
            return self._hx_destroyed or not self.winfo_exists()
        except tk.TclError:
            return True

    # size and drawing

    def _size_window(self):
        """Sized to the photo, buttons included, and at most PHOTO_SCREEN_SHARE of the screen. With an
        original on the way, the picture fills that room (the preview has the original's shape, so
        nothing moves when it arrives); with only the preview, it is shown no larger than it is."""
        _x, _y, screen_w, screen_h = _primary_screen(self)
        most_w, most_h = self._most = int(screen_w * PHOTO_SCREEN_SHARE), int(screen_h * PHOTO_SCREEN_SHARE)
        if self.image is None:
            self.geometry(f"{min(480, most_w)}x{min(320, most_h)}")
            return
        probe = 1000        # a canvas this big drives the window's size: the rest is what's around it
        self.canvas.configure(width=probe, height=probe)
        self.update_idletasks()
        around_w, around_h = self._around = self.winfo_reqwidth() - probe, self.winfo_reqheight() - probe
        room = (max(1, most_w - around_w), max(1, most_h - around_h))
        if self.original is None:
            room = (min(room[0], self.image.width), min(room[1], self.image.height))
        width, height = fit_size(self.image.size, room)
        self.canvas.configure(width=width, height=height)
        self.update_idletasks()
        self.geometry(f"{min(most_w, self.winfo_reqwidth())}x{min(most_h, self.winfo_reqheight())}")
        self.minsize(min(320, most_w), min(240, most_h))

    def _canvas_size(self):
        width, height = self.canvas.winfo_width(), self.canvas.winfo_height()
        if width <= 1 or height <= 1:       # not laid out yet
            width, height = self.canvas.winfo_reqwidth(), self.canvas.winfo_reqheight()
        return max(1, width), max(1, height)

    def _draw(self):
        """The picture fitted to the canvas, or pixel for pixel at full size."""
        self._fit_timer = None
        canvas = self.canvas
        canvas.delete("all")
        self.photo = None
        if self.image is None:
            self.shown_size = None
            return
        if self.full:
            if self._full_photo is None or self._full_photo[0] is not self.image:
                self._full_photo = (self.image, _photo_image(self.image, self))
            self.photo = self._full_photo[1]
            self.shown_size = self.image.size
            canvas.create_image(0, 0, anchor="nw", image=self.photo)
            canvas.configure(scrollregion=(0, 0, *self.image.size))
            return
        width, height = self._canvas_size()
        size = fit_size(self.image.size, (width, height))
        source = self._screen_image
        if source is None or size[0] > source.width or size[1] > source.height:
            source = self.image
        image = source if source.size == size else source.resize(size, Image.LANCZOS)
        self.photo = _photo_image(image, self)
        self.shown_size = size
        self._fitted_for = (width, height)
        canvas.create_image(width // 2, height // 2, anchor="center", image=self.photo)
        canvas.configure(scrollregion=(0, 0, width, height))
        canvas.xview_moveto(0)
        canvas.yview_moveto(0)

    def _on_resize(self, event):
        if self.full or self.image is None or (event.width, event.height) == self._fitted_for:
            return
        if self._fit_timer is not None:
            try:
                self.after_cancel(self._fit_timer)
            except tk.TclError:
                pass
        self._fit_timer = self.later(_PHOTO_FIT_MS, self._draw)

    # zoom

    def toggle_zoom(self, focus: tuple[float, float] = (0.5, 0.5)) -> None:
        """Full size and back. focus is the point of the picture (as shares of its width and height)
        that full size opens on."""
        if self.source != "original" or self.image is None:
            return
        self.full = not self.full
        self.texts.set(self.zoom_button, N_("Fit to window") if self.full else N_("Full size"))
        if self.full:
            self.xscroll.grid()
            self.yscroll.grid()
            self.canvas.configure(cursor="fleur")
            self._note(Msg(N_("Drag the photo to move around it.")))
        else:
            self.xscroll.grid_remove()
            self.yscroll.grid_remove()
            self.canvas.configure(cursor="")
            self._note(None)
        self.update_idletasks()     # the scroll bars take their room before the picture is placed
        if self.full:
            view = self._grow_for_full_size()
        elif self._fit_geometry:
            self.geometry(self._fit_geometry)
        self._draw()
        if self.full:
            width, height = view
            image_w, image_h = self.image.size
            self.canvas.xview_moveto(max(0.0, (focus[0] * image_w - width / 2) / image_w))
            self.canvas.yview_moveto(max(0.0, (focus[1] * image_h - height / 2) / image_h))

    def _grow_for_full_size(self):
        """At full size the window grows to the room the photo needs, at most PHOTO_SCREEN_SHARE of
        the screen; Fit to window brings the size before back. Returns the canvas size to expect."""
        self._fit_geometry = self.geometry()
        (most_w, most_h), (around_w, around_h) = self._most, self._around
        bars_w, bars_h = self.yscroll.winfo_reqwidth(), self.xscroll.winfo_reqheight()
        now_w, now_h = self.winfo_width(), self.winfo_height()
        if now_w <= 1 or now_h <= 1:        # not on screen yet
            now_w, now_h = self.winfo_reqwidth(), self.winfo_reqheight()
        width = max(now_w, min(most_w, self.image.width + around_w + bars_w))
        height = max(now_h, min(most_h, self.image.height + around_h + bars_h))
        if (width, height) == (now_w, now_h):
            return self._canvas_size()
        self.geometry(f"{width}x{height}")
        return max(1, width - around_w - bars_w), max(1, height - around_h - bars_h)

    def _on_double(self, event):
        if self.full or self.shown_size is None:
            self.toggle_zoom()
            return
        width, height = self._canvas_size()
        shown_w, shown_h = self.shown_size
        fx = (event.x - (width - shown_w) / 2) / shown_w
        fy = (event.y - (height - shown_h) / 2) / shown_h
        self.toggle_zoom(focus=(min(1.0, max(0.0, fx)), min(1.0, max(0.0, fy))))

    def _on_press(self, event):
        if self.full:
            self.canvas.scan_mark(event.x, event.y)

    def _on_drag(self, event):
        if self.full:
            self.canvas.scan_dragto(event.x, event.y, gain=1)

    def _wheel(self, event, axis, step):
        if not self.full:
            return None
        if not step:
            step = -int(event.delta / 120) or (-1 if event.delta > 0 else 1)
        (self.canvas.xview_scroll if axis == "x" else self.canvas.yview_scroll)(step, "units")
        return "break"

    # the original

    def _load_original(self):
        """Read the original on a worker thread (the share can take seconds); the preview stays
        until it is in."""
        services, original = self.services, self.original
        _x, _y, screen_w, screen_h = _primary_screen(self)

        def work():
            # Worker thread: the share, then Pillow. Nothing here touches Tk
            path = services.data.share_path("templates/" + original.path)
            if not path:
                return None
            with Image.open(path) as opened:
                picture = ImageOps.exif_transpose(opened)   # as the preview was made
                if picture.mode not in ("RGB", "L"):
                    picture = picture.convert("RGB")
                picture.load()
            screen = picture.copy()
            screen.thumbnail((screen_w, screen_h), Image.LANCZOS)
            return picture, screen

        def done(result, error):
            if self._gone():
                return
            if error is not None or result is None:
                self._note(Msg(N_("Showing the preview: Stripes couldn't read the original photo.")),
                           style="Warn.TLabel")
                return
            had_picture = self.image is not None
            self.image, self._screen_image = result
            self.source = "original"
            self.zoom_button.state(["!disabled"])
            self._note(None)
            if not had_picture:     # the preview was unreadable: size the window for the original
                self.picture_label.grid_remove()
                self.canvas.grid()
                self._size_window()
            self._draw()

        services.bg.run(work, done)

    def open_file(self) -> None:
        """The original in the PC's default viewer (Windows); elsewhere this only says where it is."""
        services, original = self.services, self.original
        if original is None or services is None:
            return

        def opened(_result, error):
            if error is not None and not self._gone():
                self._note(Msg(N_("Stripes couldn't open the file: {error}"),
                               {"error": str(error) or type(error).__name__}), style="Bad.TLabel")

        def found(path, error):
            if self._gone():
                return
            if error is not None or not path:
                self._note(Msg(N_("Stripes can't reach the server, so the original can't be opened now.")),
                           style="Bad.TLabel")
                return
            if not hx_win.is_windows():
                self._note(Msg(N_("Opening files works on Windows only. The file is at {path}."), {"path": path}))
                return
            services.bg.run(lambda: os.startfile(path), opened)

        services.bg.run(lambda: services.data.share_path("templates/" + original.path), found)


def show_photo(master, services, *, png: bytes, caption: str, original: "FileRef | None" = None) -> PhotoWindow:
    """A film photo in a PhotoWindow; the photo's window is brought to the front when it is open
    already (a double click on a thumbnail opens one window, not two)."""
    windows = getattr(getattr(services, "nav", None), "windows", None)
    if original is not None and isinstance(windows, dict):
        window = windows.get(f"photo:{original.sha256}")
        if isinstance(window, PhotoWindow) and not window._gone():
            window.show()
            return window
    return PhotoWindow(master, services, png=png, caption=caption, original=original)


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
