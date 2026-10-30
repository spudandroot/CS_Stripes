"""The MapleJet Send screens: the Send window, the result and confirmation window, the red failure
window, and the Stop, Resume and Set clocks dialogs.

The Send window shows each chosen line's origin in large type and every machine's change before
anything is sent; the pipeline (hx_send) does the work on a worker thread and reports through bus
events. The result window shows the printers' own pictures for the sender to confirm. The red window
is full screen and on top, and waits for Try again or Leave it.

Bag text (the origin line, what a machine prints) is only ever shown in BagText and PictureView
canvases, entries and list boxes, never in a translated label. Only the Tk thread touches widgets;
printers and the share are reached from worker threads (start_thread, services.bg.run).
"""

from __future__ import annotations

import copy
import math
import traceback
import tkinter as tk
from tkinter import ttk

import hx_message
import hx_send
import hx_state
import hx_ui_common
from hx_core import CancelToken, Msg
from hx_model import (AMBER, RED, STEP_CHECK_MACHINE, STEP_CONFIRM_2, STEP_LOCK, STEPS, ActionEvent, ActionFinished,
                      BoardUpdate, DataUpdate, HxError, LineChoice, MachineResult, MessageDesign, NotAllowed,
                      PlanError, SendEvent, SendFinished, SendRequest, SendResult, ShareError, ShareHealth,
                      new_id, same_user)
from hx_ui_common import (COLOUR_HEX, HIGHLIGHT_BG, RED_BG, RED_FG, UI_FONT, BagText, HxWindow, PhotoWindow,
                          PictureView, RecordBox, Scroller, WorkFailed, check_tk_thread, join_names, mark_done,
                          mark_running, photo_from_png, roles, start_thread, text, when)
from i18n import N_, add_spanish

ES = {
    "MapleJet Send": "Envío MapleJet",
    "Send to the bag printers": "Enviar a las impresoras de bolsas",
    "Sending as {user} on {computer}": "Enviando como {user} en {computer}",
    "Using this PC's copy of the templates, as of {time}. The server can't be reached, so every machine's result "
    "will be amber.":
        "Se usa la copia de las plantillas de esta PC, del {time}. No se puede conectar con el servidor, así que el "
        "resultado de cada máquina será ámbar.",
    "{n} log entries waiting to upload.": "{n} registros esperando para subirse.",
    "1 log entry waiting to upload.": "1 registro esperando para subirse.",
    "Lines:": "Líneas:",
    "Close the Send window? Your choices will be lost.": "¿Cerrar la ventana de envío? Se perderán sus opciones.",
    "Keep it open": "Dejarla abierta",
    "Replace the choices in the Send window?": "¿Reemplazar las opciones de la ventana de envío?",
    "Replace them": "Reemplazarlas",
    "Keep them": "Conservarlas",
    "A Send is running. Wait for it to finish.": "Hay un envío en curso. Espere a que termine.",
    "No films are ready for this line's cartridge type.":
        "No hay películas listas para el tipo de cartucho de esta línea.",
    "({min} to {max} digits)": "(de {min} a {max} dígitos)",
    "(at most {n} characters)": "(máximo {n} caracteres)",
    "(optional)": "(opcional)",
    "Countries, in the order they print:": "Países, en el orden en que se imprimen:",
    "Add": "Agregar",
    "Remove": "Quitar",
    "Origin is pre-printed on this film:": "El origen viene preimpreso en esta película:",
    "Origin on the bag:": "Origen en la bolsa:",
    "Same origin as now.": "Mismo origen que ahora.",
    "{machine} isn't ticked and is still printing:": "{machine} no está marcada y sigue imprimiendo:",
    "This line will print two origins.": "Esta línea imprimirá dos orígenes.",
    "I know this line will print two origins": "Sé que esta línea imprimirá dos orígenes",
    "(origin unknown) →": "(origen desconocido) →",
    "Prints:": "Imprime:",
    "Still needed:": "Falta:",
    "Send to {n} machines": "Enviar a {n} máquinas",
    "Send to 1 machine": "Enviar a 1 máquina",
    "{line} isn't active any more.": "{line} ya no está activa.",
    "A Send to {machines} is still running.": "Todavía hay un envío en curso a {machines}.",
    "Sending to {lines}": "Enviando a {lines}",
    "Getting ready...": "Preparando...",
    "Sending in": "Se envía en",
    "Cancelling...": "Cancelando...",
    "Hang on, we're working on it": "Espere, estamos trabajando en ello",
    "Press Cancel to stop the Send.": "Presione Cancelar para detener el envío.",
    "The Send is running. Wait for it to finish.": "El envío está en curso. Espere a que termine.",
    "Stripes hit a problem during the Send. Check each machine's card on the MapleJet tab.":
        "Stripes tuvo un problema durante el envío. Revise la tarjeta de cada máquina en la pestaña MapleJet.",
    "Check the new messages": "Revise los mensajes nuevos",
    "Sent by {user} on {computer} at {time}.": "Enviado por {user} en {computer} el {time}.",
    "Look at each printer's picture. Wait for the first bags, then confirm each line.":
        "Mire la imagen de cada impresora. Espere las primeras bolsas y luego confirme cada línea.",
    "Sent from this PC's copy of the templates (as of {time}). Check the first bags on each machine.":
        "Enviado con la copia de las plantillas de esta PC (del {time}). Revise las primeras bolsas de cada máquina.",
    "The time server didn't answer, so the PC's time was used.":
        "El servidor de hora no respondió, así que se usó la hora de la PC.",
    "The send log is waiting on this PC. It uploads when the server is back.":
        "El registro del envío está esperando en esta PC. Se subirá cuando el servidor vuelva.",
    "Live and checked twice. Waiting for you to confirm.": "Activo y revisado dos veces. Esperando su confirmación.",
    "Before:": "Antes:",
    "Same origin as before.": "Mismo origen que antes.",
    "{machine}: the Send failed. See the red window.": "{machine}: el envío falló. Vea la ventana roja.",
    "Only {user} can confirm this Send.": "Solo {user} puede confirmar este envío.",
    "Everything is right": "Todo está bien",
    "Something's wrong": "Algo está mal",
    "Confirmed by you at {time}.": "Confirmado por usted el {time}.",
    "You said something's wrong. These machines stay red until a new Send.":
        "Usted indicó que algo está mal. Estas máquinas quedan en rojo hasta un nuevo envío.",
    "Printing stopped. This line needs a new Send.": "Impresión detenida. Esta línea necesita un nuevo envío.",
    "Wait until the confirmation is saved.": "Espere a que se guarde la confirmación.",
    "Lines you haven't confirmed stay amber \"Send not confirmed\". You can confirm them later from the machine's "
    "card on the MapleJet tab.":
        "Las líneas que no confirmó quedan en ámbar \"Envío no confirmado\". Puede confirmarlas después desde la "
        "tarjeta de la máquina en la pestaña MapleJet.",
    "Loading the Send...": "Cargando el envío...",
    "This PC can't find the full record of this Send. These are the printers' latest pictures.":
        "Esta PC no encuentra el registro completo de este envío. Estas son las imágenes más recientes de las "
        "impresoras.",
    "Nothing in this Send is waiting for confirmation.": "Nada de este envío espera confirmación.",
    "Send failed": "El envío falló",
    "Sent by {user} at {time}. Stripes stopped at the step that failed and did nothing else.":
        "Enviado por {user} el {time}. Stripes se detuvo en el paso que falló y no hizo nada más.",
    "Failed at: {step}": "Falló en: {step}",
    "Where": "Dónde",
    "What": "Qué",
    "Sent": "Enviado",
    "Read back": "Leído",
    "Last confirmed printing: {name}": "Lo último confirmado que imprimía: {name}",
    "Printing before this Send (not confirmed): {name}": "Lo que imprimía antes de este envío (sin confirmar): {name}",
    "Try again was cancelled.": "Se canceló Intentar de nuevo.",
    "Stripes doesn't know what this machine was last confirmed printing.":
        "Stripes no sabe qué fue lo último confirmado que imprimía esta máquina.",
    "{machines} went live. Confirm them in the result window.":
        "{machines} quedaron activas. Confírmelas en la ventana de resultados.",
    "Only {user} can choose Try again or Leave it.": "Solo {user} puede elegir Intentar de nuevo o Dejarlo así.",
    "Choose Try again or Leave it.": "Elija Intentar de nuevo o Dejarlo así.",
    "Leave {machines} red? The line stays red until a Send succeeds, the line's notify user is alerted, and this "
    "is logged.":
        "¿Dejar {machines} en rojo? La línea queda en rojo hasta que un envío funcione, se avisa al usuario a "
        "notificar de la línea y queda registrado.",
    "Stop printing on {machines}?": "¿Detener la impresión en {machines}?",
    "You said something's wrong with the new message. Printing is turned off and read back twice. Resume stays "
    "blocked for these machines, so the rejected message can't be restarted by accident.":
        "Usted indicó que algo está mal con el mensaje nuevo. Se apaga la impresión y se revisa dos veces. Reanudar "
        "queda bloqueado en estas máquinas, para que el mensaje rechazado no se reinicie por accidente.",
    "Printing is turned off and read back twice to make sure it's off.":
        "Se apaga la impresión y se revisa dos veces para asegurar que quedó apagada.",
    "Stopping printing...": "Deteniendo la impresión...",
    "Printing stopped on {machines}. {line} needs a new Send.":
        "Se detuvo la impresión en {machines}. {line} necesita un nuevo envío.",
    "Printing stopped on {machines}.": "Se detuvo la impresión en {machines}.",
    "{machine}: {problem}": "{machine}: {problem}",
    "Wait until Stripes has finished stopping printing.": "Espere a que Stripes termine de detener la impresión.",
    "Stripes is stopping printing on {machines}.": "Stripes está deteniendo la impresión en {machines}.",
    "Stripes is resuming printing on {machines}.": "Stripes está reanudando la impresión en {machines}.",
    "Stripes is setting the clocks on {machines}.": "Stripes está ajustando los relojes en {machines}.",
    "Loading what's on each printer...": "Cargando lo que tiene cada impresora...",
    "This is the message of the last confirmed Send.": "Este es el mensaje del último envío confirmado.",
    "Only the machines that passed the check are resumed.": "Solo se reanudan las máquinas que pasaron la revisión.",
    "Printing resumed on {machines}.": "Se reanudó la impresión en {machines}.",
    "Set clocks": "Ajustar relojes",
    "Set the clock of every printer on {lines} from the time server?":
        "¿Ajustar el reloj de todas las impresoras de {lines} con el servidor de hora?",
    "Stripes sets the date, the time and the midnight date change. Nothing else on the printers changes.":
        "Stripes ajusta la fecha, la hora y el cambio de fecha a medianoche. No cambia nada más en las impresoras.",
    "Clock set": "Reloj ajustado",
}
add_spanish(ES)

# SendEvent / ActionEvent status -> (mark, label style)
MARKS = {"running": ("…", "Muted.TLabel"), "ok": ("✓", "Ok.TLabel"), "warning": ("!", "Warn.TLabel"),
         "failed": ("✗", "Bad.TLabel"), "skipped": ("–", "Muted.TLabel")}
COUNTDOWN_MS = 200
DEBOUNCE_MS = 300
THUMB_PX = 96
LOCAL_BG, LOCAL_FG = "#fef3c7", "#92400e"
BLOCKED_BG, BLOCKED_FG = "#fee2e2", "#991b1b"
_WRAP = 900
_SIDE_TEXTS = {"back": N_("Code on the back of the bag"), "front": N_("Code on the front of the bag")}
_KIND_TEXTS = {"front_photo": N_("Front photo"), "back_photo": N_("Back photo")}


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def origin_preview(film, countries: list[str]) -> str:
    """The origin line the Send window shows before the plan can be merged (bag text)."""
    if film.origin_preprinted:
        return f"{film.preprinted_origin} (pre-printed)"
    if countries:
        return hx_message.origin_phrase(countries)
    return ""


def _joined(names):
    """Names joined for a Msg value, so "and" follows the language: one name as it is, more as a
    nested "{first} and {last}"."""
    names = list(names)
    if len(names) <= 1:
        return names[0] if names else ""
    return Msg(N_("{first} and {last}"), {"first": ", ".join(names[:-1]), "last": names[-1]})


def _error_msg(error) -> Msg:
    """What to tell the operator about an exception from a worker."""
    if isinstance(error, HxError):
        return error.msg
    traceback.print_exception(error)
    return Msg(N_("Stripes hit a problem: {error}"), {"error": f"{type(error).__name__}: {error}"})


def _machine_names(services, machine_ids):
    data = services.data.current
    machines = data.machines if data is not None else {}
    return [machines[mid].name if mid in machines else mid for mid in machine_ids]


def _line_names(services, line_ids):
    data = services.data.current
    lines = data.lines if data is not None else {}
    return [lines[lid].name if lid in lines else lid for lid in line_ids]


def _lines_of(services, machine_ids):
    """The line ids of these machines, in order, each once."""
    data = services.data.current
    out = []
    for mid in machine_ids:
        machine = data.machines.get(mid) if data is not None else None
        if machine is not None and machine.line_id not in out:
            out.append(machine.line_id)
    return out


def _board_view(board, machine_id):
    if board is None:
        return None
    for line in board.lines:
        for view in line.machines:
            if view.machine_id == machine_id:
                return view
    return None


def _printer_ready(data, machine):
    printer = data.printers.get(machine.printer_id)
    return printer is not None and printer.active and printer.token != ""


def _enable(widget, on):
    widget.state(["!disabled"] if on else ["disabled"])


def _alive(widget):
    try:
        return bool(widget.winfo_exists())
    except tk.TclError:
        return False


def _full_screen(window, on=True):
    """The red windows: full screen and on top of everything."""
    window.attributes("-fullscreen", bool(on))
    window.attributes("-topmost", bool(on))


class _Lines(ttk.Label):
    """Msgs, one per line, in the current language; hidden while there are none. Grid it once
    where it belongs, then show() and hide by its Msgs."""

    def __init__(self, master, *, style="Bad.TLabel", wrap=_WRAP, **kw):
        super().__init__(master, style=style, wraplength=wrap, justify="left", **kw)
        self.msgs: list[Msg] = []

    def show(self, msgs, *, header: Msg | None = None) -> None:
        msgs = list(msgs)
        self.msgs = ([header] if header is not None and msgs else []) + msgs
        self.render()

    def render(self) -> None:
        check_tk_thread()
        self.configure(text="\n".join(text(m) for m in self.msgs))
        if self.msgs:
            self.grid()
        else:
            self.grid_remove()


def _render_lines(widget):
    """Every _Lines inside widget in the current language (not inside another window)."""
    for child in widget.winfo_children():
        if isinstance(child, HxWindow):
            continue
        if isinstance(child, _Lines):
            child.render()
        _render_lines(child)


def _clear(frame, texts):
    """Destroy a frame's children and drop their Texts entries."""
    def forget(widget):
        for child in widget.winfo_children():
            forget(child)
        texts.forget(widget)
    for child in frame.winfo_children():
        forget(child)
        child.destroy()


def _red_label(master, size=14, bold=False, wrap=1400):
    return tk.Label(master, background=RED_BG, foreground=RED_FG, font=(UI_FONT, size, "bold" if bold else "normal"),
                    wraplength=wrap, justify="left", anchor="w")


# --------------------------------------------------------------------------
# The Send window
# --------------------------------------------------------------------------

class ProgressRow(ttk.Frame):
    """One machine on the Send window's progress page: name, mark, step, message and its last picture."""

    def __init__(self, master, window: "SendWindow", name: str):
        super().__init__(master, padding=(0, 3))
        self.window = window
        self.name_label = ttk.Label(self, text=name, width=24)     # a record name: data
        self.name_label.grid(row=0, column=0, sticky="nw")
        self.mark_label = ttk.Label(self, text=MARKS["running"][0], style=MARKS["running"][1], width=2)
        self.mark_label.grid(row=0, column=1, sticky="nw")
        self.step_label = ttk.Label(self, wraplength=360, justify="left")
        self.step_label.grid(row=0, column=2, sticky="nw")
        self.msg_label = ttk.Label(self, wraplength=360, justify="left", style="Muted.TLabel")
        self.msg_label.grid(row=1, column=2, sticky="nw")
        self.picture = PictureView(self, width=256)
        self.picture.grid(row=0, column=3, rowspan=2, sticky="ne", padx=(12, 0))
        self.picture.grid_remove()
        self.columnconfigure(2, weight=1)

    def show_event(self, event) -> None:
        mark, style = MARKS.get(event.status, MARKS["running"])
        self.mark_label.configure(text=mark, style=style)
        texts = self.window.texts
        texts.msg(self.step_label, hx_message.STEP_TEXTS.get(event.step, Msg(event.step)))
        texts.msg(self.msg_label, event.msg)
        self.msg_label.configure(style=style if event.msg is not None else "Muted.TLabel")
        if event.design is not None:
            self.picture.set_bmp(event.design.preview_bmp)
            self.picture.grid()


class LineBlock(ttk.LabelFrame):
    """One chosen line in the Send window: machines, then product, film and print text, what the
    sender fills in, the origin in large type and each machine's preview (ui.md §5.3, §5.4)."""

    def __init__(self, master, window: "SendWindow", line):
        super().__init__(master, text=line.name, padding=10)     # a record name: data
        self.window = window
        self.services = window.services
        self.line = line
        self.machine_vars: dict[str, tk.BooleanVar] = {}
        self.machine_checks: dict[str, ttk.Checkbutton] = {}
        self.state_labels: dict[str, tk.Label] = {}
        self.film_var = tk.StringVar(value="")
        self.film_radios: dict[str, ttk.Radiobutton] = {}
        self.thumbs: dict[tuple[str, str], ttk.Label] = {}
        self.lot_var = tk.StringVar(value="")
        self.countries: list[str] = []
        self.free_entries: dict[str, ttk.Entry] = {}
        self.change_texts: dict[str, BagText] = {}
        self.bag_texts: dict[str, BagText] = {}
        self.position_labels: dict[str, ttk.Label] = {}
        self.warning_labels: dict[str, list[ttk.Label]] = {}
        self.unticked_warnings: dict[str, tk.Frame] = {}
        self.two_origins_var = tk.BooleanVar(value=False)
        self.trial = None
        self._free_vars: dict[str, tk.StringVar] = {}
        self._photos = {}           # (film id, kind) -> (PhotoImage, png)
        self._has_lot = False
        self._warned = frozenset()
        self._suspend = 0
        self._debounce_timer = None
        self._signature = None
        self.columnconfigure(1, weight=1)
        self._build()
        self.lot_var.trace_add("write", lambda *a: self._debounce())

    # building

    def _row_label(self, row, english):
        label = ttk.Label(self, style="Hx.Section.TLabel")
        self.window.texts.set(label, english)
        label.grid(row=row, column=0, sticky="nw", padx=(0, 10), pady=3)
        return label

    def _build(self):
        texts = self.window.texts
        data = self.services.data.current
        self._signature = self._data_signature(data)

        # 1 Machines
        self._row_label(0, N_("Machines"))
        self.machines_frame = ttk.Frame(self)
        self.machines_frame.grid(row=0, column=1, sticky="w", pady=3)
        for i, machine in enumerate(data.line_machines(self.line.id)):
            var = tk.BooleanVar(value=True)
            check = ttk.Checkbutton(self.machines_frame, text=machine.name, variable=var, command=self._changed)
            check.grid(row=i // 2, column=(i % 2) * 2, sticky="w")
            state = tk.Label(self.machines_frame, anchor="w")
            state.grid(row=i // 2, column=(i % 2) * 2 + 1, sticky="w", padx=(4, 24))
            self.machine_vars[machine.id] = var
            self.machine_checks[machine.id] = check
            self.state_labels[machine.id] = state
        self.update_board()

        # 2 Product
        self.product_label = self._row_label(1, N_("Product"))
        self.product_box = RecordBox(self, width=44)
        self.product_box.grid(row=1, column=1, sticky="w", pady=3)
        self.product_box.bind("<<ComboboxSelected>>", lambda e: self._product_chosen(), add="+")
        self.no_films_label = ttk.Label(self, style="Warn.TLabel")
        texts.set(self.no_films_label, N_("No films are ready for this line's cartridge type."))
        self.no_films_label.grid(row=1, column=1, sticky="w", pady=3)
        products = self._products(data)
        self.product_box.set_records([(p.id, p.name) for p in products])
        if products:
            self.no_films_label.grid_remove()
        else:
            self.product_box.grid_remove()

        # 3 Film
        self.film_label = self._row_label(2, N_("Film"))
        self.films_frame = ttk.Frame(self)
        self.films_frame.grid(row=2, column=1, sticky="w", pady=3)

        # 4 Print text
        self.print_text_label = self._row_label(3, N_("Print text"))
        self.print_text_box = RecordBox(self, width=44)
        self.print_text_box.grid(row=3, column=1, sticky="w", pady=3)
        self.print_text_box.bind("<<ComboboxSelected>>", lambda e: self._print_text_chosen(), add="+")

        # 5 What the sender fills in
        self.lot_label = self._row_label(4, N_("Lot"))
        self.lot_frame = ttk.Frame(self)
        self.lot_frame.grid(row=4, column=1, sticky="w", pady=3)
        self.lot_entry = ttk.Entry(self.lot_frame, width=12, textvariable=self.lot_var)
        self.lot_entry.pack(side="left")
        self.lot_hint = ttk.Label(self.lot_frame, style="Muted.TLabel")
        self.lot_hint.pack(side="left", padx=(8, 0))

        self.countries_frame = ttk.Frame(self)
        self.countries_frame.grid(row=5, column=0, columnspan=2, sticky="w", pady=3)
        caption = ttk.Label(self.countries_frame)
        texts.set(caption, N_("Countries, in the order they print:"))
        caption.grid(row=0, column=0, columnspan=4, sticky="w")
        self.country_list = tk.Listbox(self.countries_frame, height=4, width=24, exportselection=False)
        self.country_list.grid(row=1, column=0, rowspan=3, sticky="nw", padx=(20, 6))
        self.up_button = ttk.Button(self.countries_frame, command=lambda: self._move_country(-1))
        texts.set(self.up_button, N_("Move up"))
        self.up_button.grid(row=1, column=1, sticky="ew")
        self.down_button = ttk.Button(self.countries_frame, command=lambda: self._move_country(1))
        texts.set(self.down_button, N_("Move down"))
        self.down_button.grid(row=2, column=1, sticky="ew")
        self.remove_country_button = ttk.Button(self.countries_frame, command=self._remove_country)
        texts.set(self.remove_country_button, N_("Remove"))
        self.remove_country_button.grid(row=3, column=1, sticky="ew")
        self.country_box = ttk.Combobox(self.countries_frame, state="readonly", width=24)
        self.country_box.grid(row=1, column=2, sticky="nw", padx=(16, 4))
        self.add_country_button = ttk.Button(self.countries_frame, command=self._add_country)
        texts.set(self.add_country_button, N_("Add"))
        self.add_country_button.grid(row=1, column=3, sticky="nw")

        self.preprinted_frame = ttk.Frame(self)
        self.preprinted_frame.grid(row=6, column=0, columnspan=2, sticky="w", pady=3)
        caption = ttk.Label(self.preprinted_frame)
        texts.set(caption, N_("Origin is pre-printed on this film:"))
        caption.pack(side="left")
        self.preprinted_text = BagText(self.preprinted_frame, size=13, bold=True)
        self.preprinted_text.pack(side="left", padx=(8, 0))

        self.free_frame = ttk.Frame(self)
        self.free_frame.grid(row=7, column=0, columnspan=2, sticky="w", pady=3)

        # 6 Origin and preview
        self.preview_frame = ttk.Frame(self)
        self.preview_frame.grid(row=8, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Separator(self.preview_frame).pack(fill="x", pady=(0, 6))
        caption = ttk.Label(self.preview_frame, style="Hx.Section.TLabel")
        texts.set(caption, N_("Origin on the bag:"))
        caption.pack(anchor="w")
        self.origin_text = BagText(self.preview_frame, size=20, bold=True, wrap_px=760)
        self.origin_text.pack(anchor="w", padx=(20, 0))
        self.machines_preview = ttk.Frame(self.preview_frame)
        self.machines_preview.pack(fill="x", padx=(20, 0))
        self.warnings_frame = ttk.Frame(self)
        self.warnings_frame.grid(row=9, column=0, columnspan=2, sticky="ew")
        self.two_origins_check = ttk.Checkbutton(self, variable=self.two_origins_var,
                                                 command=self.window.update_send_button)
        texts.set(self.two_origins_check, N_("I know this line will print two origins"))
        self.two_origins_check.grid(row=10, column=0, columnspan=2, sticky="w")
        self.problems_label = _Lines(self, style="Warn.TLabel")
        self.problems_label.grid(row=11, column=0, columnspan=2, sticky="w", pady=(6, 0))

        self._show_rows()

    @staticmethod
    def _data_signature(data):
        """What the block is built from: when none of it changes, a DataUpdate rebuilds nothing."""
        if data is None:
            return None
        return (data.company, data.lines, data.machines, data.printers, data.products, data.films,
                data.print_texts, data.adjustments, data.calibrations)

    def _products(self, data):
        found = [p for p in data.products.values()
                 if p.active and hx_message.films_for_line(self.line, p.id, data)]
        return sorted(found, key=lambda p: (hx_state.natural_key(p.name), p.id))

    def _film(self):
        data = self.services.data.current
        film_id = self.film_var.get()
        return data.films.get(film_id) if data is not None and film_id else None

    def _print_text(self):
        data = self.services.data.current
        pt_id = self.print_text_box.record_id()
        return data.print_texts.get(pt_id) if data is not None and pt_id else None

    def _build_films(self):
        """One row per film of the chosen product: radio, side, front and back photos."""
        texts = self.window.texts
        _clear(self.films_frame, texts)
        self.film_radios.clear()
        self.thumbs.clear()
        self._photos.clear()
        data = self.services.data.current
        product_id = self.product_box.record_id()
        if not product_id:
            return
        loads = []
        for row, film in enumerate(hx_message.films_for_line(self.line, product_id, data)):
            name = f"{film.code} · {film.artwork_revision}" if film.artwork_revision else film.code
            radio = ttk.Radiobutton(self.films_frame, text=name, value=film.id, variable=self.film_var,
                                    command=self._film_chosen)
            radio.grid(row=row, column=0, sticky="w", pady=2)
            self.film_radios[film.id] = radio
            side = ttk.Label(self.films_frame, style="Muted.TLabel")
            texts.set(side, _SIDE_TEXTS.get(film.side, _SIDE_TEXTS["back"]))
            side.grid(row=row, column=1, sticky="w", padx=(12, 12))
            for col, kind in ((2, "front_photo"), (3, "back_photo")):
                thumb = ttk.Label(self.films_frame, cursor="hand2")
                thumb.grid(row=row, column=col, sticky="w", padx=4, pady=2)
                self.thumbs[(film.id, kind)] = thumb
                refs = [f for f in film.files if f.kind == kind]
                if refs:
                    loads.append((film, kind, refs[-1].sha256))
                else:
                    thumb.configure(style="Muted.TLabel")
                    texts.set(thumb, N_("No photo"))
        if loads:
            # Photos come from the PC's cache or the share: a worker each, never from a constructor
            self.window.later(0, lambda: [self._load_thumb(*load) for load in loads])

    def _load_thumb(self, film, kind, sha256):
        services = self.services
        key = (film.id, kind)

        def done(png, error):
            thumb = self.thumbs.get(key)
            if thumb is None or not _alive(thumb):
                return
            photo = None
            if png and error is None:
                try:
                    photo = photo_from_png(thumb, png, height=THUMB_PX)
                except Exception:
                    photo = None
            if photo is None:
                thumb.configure(style="Muted.TLabel", image="")
                self.window.texts.set(thumb, N_("No photo"))
                return
            self.window.texts.forget(thumb)
            self._photos[key] = (photo, png)
            thumb.configure(image=photo, text="")
            caption = f"{film.code} · {film.artwork_revision}: " if film.artwork_revision else f"{film.code}: "
            thumb.bind("<Button-1>", lambda e: PhotoWindow(self.window.master, services, png=png,
                                                           caption=caption + text(Msg(_KIND_TEXTS[kind]))))

        services.bg.run(lambda: services.data.preview_bytes(film.id, sha256), done)

    def _build_fill_in(self):
        """Lot, countries or the pre-printed origin, and free texts, for the chosen print text."""
        texts = self.window.texts
        data = self.services.data.current
        film, pt = self._film(), self._print_text()
        company = data.company
        kinds = {it.kind for it in pt.items} if pt is not None else set()
        self._has_lot = pt is not None and "lot" in kinds
        texts.set(self.lot_hint, N_("({min} to {max} digits)"), min=company.lot_min_digits,
                  max=company.lot_max_digits)
        _clear(self.free_frame, texts)
        self.free_entries.clear()
        if pt is not None:
            row = 0
            for item in pt.items:
                if item.kind != "free_text":
                    continue
                ttk.Label(self.free_frame, text=item.label).grid(row=row, column=0, sticky="w", padx=(0, 8))
                var = self._free_vars.get(item.id)
                if var is None:
                    var = self._free_vars[item.id] = tk.StringVar(value="")
                    var.trace_add("write", lambda *a: self._debounce())
                entry = ttk.Entry(self.free_frame, width=min(40, item.max_len + 2), textvariable=var)
                entry.grid(row=row, column=1, sticky="w", pady=2)
                self.free_entries[item.id] = entry
                hint = ttk.Label(self.free_frame, style="Muted.TLabel")
                texts.set(hint, N_("(at most {n} characters)"), n=item.max_len)
                hint.grid(row=row, column=2, sticky="w", padx=(8, 0))
                if not item.required:
                    optional = ttk.Label(self.free_frame, style="Muted.TLabel")
                    texts.set(optional, N_("(optional)"))
                    optional.grid(row=row, column=3, sticky="w", padx=(6, 0))
                row += 1
        self._show_countries()

    def _show_countries(self):
        data = self.services.data.current
        self.country_list.delete(0, "end")
        for name in self.countries:
            self.country_list.insert("end", name)
        offered = [c.name for c in data.company.countries if c.active and c.name not in self.countries]
        self.country_box.configure(values=offered)
        if self.country_box.get() not in offered:
            self.country_box.set(offered[0] if offered else "")

    def _show_rows(self):
        """A row shows only when the one before it has a value."""
        product = bool(self.product_box.record_id())
        film = self._film() if product else None
        pt = self._print_text() if film is not None else None
        for widget in (self.film_label, self.films_frame):
            widget.grid() if product else widget.grid_remove()
        for widget in (self.print_text_label, self.print_text_box):
            widget.grid() if film is not None else widget.grid_remove()
        kinds = {it.kind for it in pt.items} if pt is not None else set()
        for widget in (self.lot_label, self.lot_frame):
            widget.grid() if pt is not None and "lot" in kinds else widget.grid_remove()
        preprinted = film is not None and film.origin_preprinted
        if pt is not None and "origin" in kinds and not preprinted:
            self.countries_frame.grid()
        else:
            self.countries_frame.grid_remove()
        if pt is not None and preprinted:
            self.preprinted_text.set(origin_preview(film, []))
            self.preprinted_frame.grid()
        else:
            self.preprinted_frame.grid_remove()
        self.free_frame.grid() if pt is not None and self.free_entries else self.free_frame.grid_remove()
        self.preview_frame.grid() if film is not None else self.preview_frame.grid_remove()

    # choosing

    def _product_chosen(self):
        if self._suspend:
            return
        self.film_var.set("")
        self.print_text_box.set_records([])
        self._build_films()
        self._build_fill_in()
        self._show_rows()
        self._changed()

    def _offer_print_texts(self, film):
        data = self.services.data.current
        pts = hx_message.print_texts_for_film(film, data) if film is not None else []
        pts = sorted(pts, key=lambda p: (hx_state.natural_key(p.name), p.id))
        self.print_text_box.set_records([(p.id, p.name) for p in pts])
        return [p.id for p in pts]

    def _film_chosen(self):
        if self._suspend:
            return
        film = self._film()
        offered = self._offer_print_texts(film)
        # The print text defaults to the film's (plan)
        default = film.default_print_text_id if film is not None else ""
        self.print_text_box.set_record_id(default if default in offered else "")
        if film is not None and film.origin_preprinted:
            self.countries = []
        self._build_fill_in()
        self._show_rows()
        self._changed()

    def _print_text_chosen(self):
        if self._suspend:
            return
        self._build_fill_in()
        self._show_rows()
        self._changed()

    def _add_country(self):
        name = self.country_box.get()
        if name and name not in self.countries:
            self.countries.append(name)
            self._show_countries()
            self._changed()

    def _selected_country(self):
        chosen = self.country_list.curselection()
        return chosen[0] if chosen else None

    def _move_country(self, step):
        i = self._selected_country()
        if i is None or not 0 <= i + step < len(self.countries):
            return
        self.countries[i], self.countries[i + step] = self.countries[i + step], self.countries[i]
        self._show_countries()
        self.country_list.selection_set(i + step)
        self._changed()

    def _remove_country(self):
        i = self._selected_country()
        if i is None:
            return
        del self.countries[i]
        self._show_countries()
        self._changed()

    # the choice

    def choice(self) -> LineChoice:
        film = self._film()
        pt = self._print_text()
        free = {}
        if pt is not None:
            for item in pt.items:
                if item.kind == "free_text" and item.id in self._free_vars:
                    free[item.id] = self._free_vars[item.id].get()
        return LineChoice(
            line_id=self.line.id,
            machine_ids=[mid for mid, var in self.machine_vars.items() if var.get()],
            product_id=self.product_box.record_id(), film_id=self.film_var.get(),
            print_text_id=self.print_text_box.record_id(),
            lot=self.lot_entry.get() if self._has_lot else "",
            countries=[] if film is not None and film.origin_preprinted else list(self.countries),
            free_text=free)

    def fill(self, choice: LineChoice) -> None:
        """Ticked machines, product, film, print text, lot, countries and free text, in that order. A
        value no longer offered is left empty, so problems() shows what is missing."""
        data = self.services.data.current
        self._suspend += 1
        try:
            for mid, var in self.machine_vars.items():
                check = self.machine_checks[mid]
                var.set(mid in choice.machine_ids and not check.instate(["disabled"]))
            products = [p.id for p in self._products(data)]
            self.product_box.set_record_id(choice.product_id if choice.product_id in products else "")
            self._build_films()
            film_id = choice.film_id if choice.film_id in self.film_radios else ""
            self.film_var.set(film_id)
            offered = self._offer_print_texts(self._film())
            self.print_text_box.set_record_id(choice.print_text_id if choice.print_text_id in offered else "")
            self.lot_var.set(choice.lot or "")
            active = {c.name for c in data.company.countries if c.active}
            countries = []
            for name in choice.countries:
                if name in active and name not in countries:
                    countries.append(name)
            self.countries = countries
            for item_id, value in (choice.free_text or {}).items():
                var = self._free_vars.get(item_id)
                if var is None:
                    var = self._free_vars[item_id] = tk.StringVar(value="")
                    var.trace_add("write", lambda *a: self._debounce())
                var.set(value)
            self._build_fill_in()
            self._show_rows()
        finally:
            self._suspend -= 1
        self.recompute()

    def problems(self) -> list[Msg]:
        services = self.services
        return hx_message.validate_choice(self.choice(), services.data.current, services.identity)

    def has_product(self) -> bool:
        return bool(self.product_box.record_id())

    def ticked(self) -> int:
        return sum(1 for var in self.machine_vars.values() if var.get())

    def ready(self) -> bool:
        """No problems, a trial plan, and the two origins acknowledged when there are warnings."""
        return self.trial is not None and (not self.unticked_warnings or bool(self.two_origins_var.get()))

    # updates

    def update_board(self) -> None:
        """Machine states and their colours from the latest board; a machine with no printer set up
        is unticked and can't be ticked."""
        data = self.services.data.current
        for mid, label in self.state_labels.items():
            view = self.window.view(mid)
            machine = data.machines.get(mid)
            not_set_up = (view.code == "not_set_up" if view is not None
                          else machine is None or not _printer_ready(data, machine))
            check = self.machine_checks[mid]
            if not_set_up:
                self.machine_vars[mid].set(False)
                _enable(check, False)
            else:
                _enable(check, True)
            if view is not None:
                label.configure(foreground=COLOUR_HEX.get(view.colour, COLOUR_HEX[AMBER]))
                self.window.texts.msg(label, view.headline)
            else:
                self.window.texts.forget(label)
                label.configure(text="")

    def data_changed(self) -> None:
        """A DataUpdate: rebuilt with the choice kept when anything it shows has changed."""
        data = self.services.data.current
        signature = self._data_signature(data)
        line = data.lines.get(self.line.id) if data is not None else None
        if data is None or signature == self._signature:
            self.recompute()
            return
        choice = self.choice()
        if line is not None:
            self.line = line
            self.configure(text=line.name)
        _clear(self, self.window.texts)
        self.machine_vars.clear()
        self.machine_checks.clear()
        self.state_labels.clear()
        self.change_texts.clear()
        self.bag_texts.clear()
        self.position_labels.clear()
        self.warning_labels.clear()
        self.unticked_warnings.clear()
        self._build()
        self.fill(choice)

    def _debounce(self):
        if self._suspend:
            return
        if self._debounce_timer is not None:
            try:
                self.after_cancel(self._debounce_timer)
            except tk.TclError:
                pass
        self._debounce_timer = self.window.later(DEBOUNCE_MS, self.recompute)

    def _changed(self):
        if not self._suspend:
            self.recompute()

    def recompute(self) -> None:
        """problems(), then the trial plan and each machine's preview, then the warnings and the
        window's Send button."""
        self._debounce_timer = None
        services = self.services
        data = services.data.current
        if data is None:
            return
        film = self._film()
        self.origin_text.set(origin_preview(film, self.countries) if film is not None else "")
        texts = self.window.texts
        _clear(self.machines_preview, texts)
        self.change_texts.clear()
        self.bag_texts.clear()
        self.position_labels.clear()
        self.warning_labels.clear()
        self.trial = None
        problems = self.problems()
        if problems:
            self.problems_label.show(problems, header=Msg(N_("Still needed:")))
        else:
            identity = services.identity
            request = SendRequest(send_id="sd_preview", sender=identity.user, computer=identity.computer,
                                  created_at=services.clock.time(), lines=[self.choice()])
            try:
                self.trial = hx_message.merge_plan(request, data, identity)
            except PlanError as e:
                self.problems_label.show(e.problems)
            except NotAllowed as e:
                self.problems_label.show([e.msg])
            else:
                self.problems_label.show([])
                self._show_trial()
        self._show_warnings(film)
        self.window.update_send_button()

    def _show_trial(self):
        texts = self.window.texts
        frame = self.machines_preview
        for row_plan in self.trial.machines:
            mid = row_plan.machine.id
            view = self.window.view(mid)
            old = view.origin_line if view is not None else ""
            row = ttk.Frame(frame, padding=(0, 4))
            row.pack(fill="x", anchor="w")
            ttk.Label(row, text=row_plan.machine.name, width=22).grid(row=0, column=0, sticky="nw")
            change = hx_message.origin_change_text(old, row_plan.origin_line)
            change_row = ttk.Frame(row)
            change_row.grid(row=0, column=1, sticky="w")
            if not old:
                unknown = ttk.Label(change_row, style="Muted.TLabel")
                texts.set(unknown, N_("(origin unknown) →"))
                unknown.pack(side="left")
                shown = BagText(change_row, size=11, bold=True, background=HIGHLIGHT_BG)
                shown.set(hx_message.origin_change_text("", row_plan.origin_line))
                shown.pack(side="left", padx=(4, 0))
                self.change_texts[mid] = shown
            elif change:
                shown = BagText(change_row, size=11, bold=True, background=HIGHLIGHT_BG)
                shown.set(change)
                shown.pack(side="left")
                self.change_texts[mid] = shown
            else:
                same = ttk.Label(change_row, style="Muted.TLabel")
                texts.set(same, N_("Same origin as now."))
                same.pack(side="left")
            prints = ttk.Frame(row)
            prints.grid(row=1, column=1, sticky="w")
            caption = ttk.Label(prints)
            texts.set(caption, N_("Prints:"))
            caption.pack(side="left", anchor="n")
            bag = BagText(prints, size=11, wrap_px=620)
            bag.set(row_plan.bag_text)
            bag.pack(side="left", padx=(4, 0))
            self.bag_texts[mid] = bag
            position = ttk.Label(row, text=row_plan.position.text, style="Muted.TLabel")   # English working: data
            position.grid(row=2, column=1, sticky="w")
            self.position_labels[mid] = position
            warnings = []
            for n, warning in enumerate(row_plan.warnings):
                label = ttk.Label(row, style="Warn.TLabel", wraplength=620, justify="left")
                texts.msg(label, warning)
                label.grid(row=3 + n, column=1, sticky="w")
                warnings.append(label)
            self.warning_labels[mid] = warnings

    def _show_warnings(self, film):
        """Unticked machines that are still printing another origin (ui.md §5.4 item 4)."""
        texts = self.window.texts
        _clear(self.warnings_frame, texts)
        self.unticked_warnings.clear()
        warned = []
        if film is not None:
            origin = origin_preview(film, self.countries)
            for mid, var in self.machine_vars.items():
                if var.get():
                    continue
                view = self.window.view(mid)
                printing = view is None or (view.code != "off" and view.printing is not False)
                differs = (view is None or view.origin_line == ""
                           or hx_message.origin_change_text(view.origin_line, origin) != "")
                if printing and differs:
                    warned.append((mid, view))
        data = self.services.data.current
        for mid, view in warned:
            name = data.machines[mid].name if mid in data.machines else mid
            row = tk.Frame(self.warnings_frame, background=HIGHLIGHT_BG, padx=6, pady=4)
            row.pack(fill="x", anchor="w", pady=2)
            label = tk.Label(row, background=HIGHLIGHT_BG, foreground=COLOUR_HEX[AMBER])
            texts.set(label, N_("{machine} isn't ticked and is still printing:"), machine=name)
            label.pack(side="left")
            if view is not None and view.origin_line:
                origin_text = BagText(row, size=11, bold=True, background=HIGHLIGHT_BG)
                origin_text.set(view.origin_line)
                origin_text.pack(side="left", padx=(6, 6))
            else:
                unknown = tk.Label(row, background=HIGHLIGHT_BG, foreground=COLOUR_HEX[AMBER])
                texts.set(unknown, N_("(origin unknown)"))
                unknown.pack(side="left", padx=(6, 6))
            two = tk.Label(row, background=HIGHLIGHT_BG, foreground=COLOUR_HEX[AMBER])
            texts.set(two, N_("This line will print two origins."))
            two.pack(side="left")
            self.unticked_warnings[mid] = row
        now = frozenset(self.unticked_warnings)
        if now != self._warned:
            self.two_origins_var.set(False)
            self._warned = now
        if self.unticked_warnings:
            self.two_origins_check.grid()
        else:
            self.two_origins_check.grid_remove()

    def destroy(self) -> None:
        if self._debounce_timer is not None:
            try:
                self.after_cancel(self._debounce_timer)
            except tk.TclError:
                pass
        super().destroy()


class SendWindow(HxWindow):
    """Send to the bag printers: choose, check the origin and every change, send, and watch it run."""
    key = "send"

    def __init__(self, master, services, *, prefill: SendRequest | None = None, line_ids: list[str] | None = None):
        super().__init__(master, services, title=N_("MapleJet Send"))
        self.geometry("1000x780")
        self.minsize(760, 560)
        self.page = "choose"
        self.line_vars: dict[str, tk.BooleanVar] = {}
        self.line_checks: dict[str, ttk.Checkbutton] = {}
        self.blocks: dict[str, LineBlock] = {}
        self.kept: dict[str, LineChoice] = {}
        self.plan = None
        self.cancel_token = None
        self.progress_rows: dict[str, ProgressRow] = {}
        self.countdown_origins: dict[str, BagText] = {}
        self.board = services.watcher.latest
        self._notes: list[Msg] = []         # the window's own problems (a plan error, inactive lines)
        self._countdown_at = None
        self._countdown_timer = None
        self._work_failed = False
        self._pending_logs = 0
        self._build()
        self.subscribe(DataUpdate, self._on_data)
        self.subscribe(ShareHealth, lambda e: self._show_banners())
        self.subscribe(BoardUpdate, self._on_board)
        self.subscribe(SendEvent, self._on_send_event)
        self.subscribe(SendFinished, self._on_finished)
        self.subscribe(WorkFailed, self._on_work_failed)
        self.later(0, self._load_queue)
        if prefill is not None:
            self.fill(prefill)
        elif line_ids:
            self.tick_lines(line_ids)
        self._show_banners()
        self.update_send_button()

    # building

    def _build(self):
        texts = self.texts
        identity = self.services.identity
        self.body = ttk.Frame(self, padding=12)
        self.body.pack(fill="both", expand=True)
        self.body.columnconfigure(0, weight=1)
        self.body.rowconfigure(0, weight=1)

        # The choice page
        page = self.choose_page = ttk.Frame(self.body)
        page.grid(row=0, column=0, sticky="nsew")
        page.columnconfigure(0, weight=1)
        page.rowconfigure(6, weight=1)
        head = ttk.Frame(page)
        head.grid(row=0, column=0, sticky="ew")
        self.heading_label = ttk.Label(head, style="Hx.Head.TLabel")
        texts.set(self.heading_label, N_("Send to the bag printers"))
        self.heading_label.pack(side="left")
        self.as_label = ttk.Label(head, style="Muted.TLabel")
        texts.set(self.as_label, N_("Sending as {user} on {computer}"), user=identity.user,
                  computer=identity.computer)
        self.as_label.pack(side="right")

        self.local_banner = tk.Frame(page, background=LOCAL_BG, padx=10, pady=6)
        self.local_banner.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        self.local_label = tk.Label(self.local_banner, background=LOCAL_BG, foreground=LOCAL_FG, wraplength=900,
                                    justify="left", anchor="w")
        self.local_label.pack(fill="x")
        self.blocked_banner = tk.Frame(page, background=BLOCKED_BG, padx=10, pady=6)
        self.blocked_banner.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        self.blocked_label = tk.Label(self.blocked_banner, background=BLOCKED_BG, foreground=BLOCKED_FG,
                                      wraplength=760, justify="left", anchor="w")
        self.blocked_label.pack(side="left", fill="x", expand=True)
        self.retry_button = ttk.Button(self.blocked_banner, command=self._retry)
        texts.set(self.retry_button, N_("Retry"))
        self.retry_button.pack(side="right")
        self.queue_label = ttk.Label(page, style="Muted.TLabel")
        self.queue_label.grid(row=3, column=0, sticky="w", pady=(6, 0))
        self.queue_label.grid_remove()
        self.problems_label = _Lines(page, style="Warn.TLabel")
        self.problems_label.grid(row=4, column=0, sticky="w", pady=(6, 0))
        self.problems_label.show([])

        self.lines_row = ttk.Frame(page)
        self.lines_row.grid(row=5, column=0, sticky="ew", pady=(8, 4))
        self._build_line_checks()

        self.scroller = Scroller(page)
        self.scroller.grid(row=6, column=0, sticky="nsew")
        self.blocks_frame = self.scroller.inner

        buttons = ttk.Frame(page)
        buttons.grid(row=8, column=0, sticky="ew", pady=(10, 0))
        self.close_button = ttk.Button(buttons, command=self.close)
        texts.set(self.close_button, N_("Close"))
        self.close_button.pack(side="left")
        self.send_button = ttk.Button(buttons, style="Hx.Big.TButton", command=self._send)
        self.send_button.pack(side="right")

        # The progress page
        page = self.progress_page = ttk.Frame(self.body)
        page.grid(row=0, column=0, sticky="nsew")
        page.columnconfigure(0, weight=1)
        page.rowconfigure(5, weight=1)
        self.progress_heading = ttk.Label(page, style="Hx.Head.TLabel")
        self.progress_heading.grid(row=0, column=0, sticky="w")
        self.countdown_frame = ttk.Frame(page)
        self.countdown_frame.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        self.countdown_frame.columnconfigure(0, weight=1)
        self.origins_frame = ttk.Frame(self.countdown_frame)
        self.origins_frame.grid(row=0, column=0, sticky="w")
        self.countdown_caption = ttk.Label(self.countdown_frame, font=(UI_FONT, 14))
        self.countdown_caption.grid(row=1, column=0, pady=(12, 0))
        self.countdown_label = ttk.Label(self.countdown_frame, font=(UI_FONT, 48, "bold"))
        self.countdown_label.grid(row=2, column=0)
        self.cancel_button = ttk.Button(self.countdown_frame, style="Hx.Big.TButton", command=self._cancel)
        texts.set(self.cancel_button, N_("Cancel"))
        self.cancel_button.grid(row=3, column=0, pady=(4, 8))
        self.working_label = ttk.Label(page, font=(UI_FONT, 18, "bold"))
        texts.set(self.working_label, N_("Hang on, we're working on it"))
        self.working_label.grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.working_label.grid_remove()
        self.work_problem_label = _Lines(page, style="Bad.TLabel")
        self.work_problem_label.grid(row=4, column=0, sticky="w", pady=(6, 0))
        self.work_problem_label.show([])
        self.rows_scroller = Scroller(page)
        self.rows_scroller.grid(row=5, column=0, sticky="nsew", pady=(8, 0))
        self.progress_close_button = ttk.Button(page, command=self.close)
        texts.set(self.progress_close_button, N_("Close"))
        self.progress_close_button.grid(row=6, column=0, sticky="e", pady=(8, 0))
        self.progress_close_button.grid_remove()
        page.grid_remove()

        # Made after both pages, so it shows on top of whichever page holds it
        self.send_status_label = ttk.Label(self.body, wraplength=900, justify="left")
        self._place_status(self.choose_page)

    def _place_status(self, page):
        row = 7 if page is self.choose_page else 3
        self.send_status_label.grid(in_=page, row=row, column=0, sticky="w", pady=(6, 0))
        if not self.send_status_label.cget("text"):
            self.send_status_label.grid_remove()

    def _set_status(self, msg: Msg | None, style="TLabel"):
        self.texts.msg(self.send_status_label, msg)
        self.send_status_label.configure(style=style)
        if msg is None:
            self.send_status_label.grid_remove()
        else:
            self.send_status_label.grid()

    def _active_lines(self):
        data = self.services.data.current
        if data is None:
            return []
        lines = [ln for ln in data.lines.values() if ln.active]
        return sorted(lines, key=lambda ln: (hx_state.natural_key(ln.name), ln.id))

    def _build_line_checks(self):
        _clear(self.lines_row, self.texts)
        self.line_checks.clear()
        caption = ttk.Label(self.lines_row, style="Hx.Section.TLabel")
        self.texts.set(caption, N_("Lines:"))
        caption.pack(side="left", padx=(0, 10))
        for line in self._active_lines():
            var = self.line_vars.get(line.id)
            if var is None:
                var = self.line_vars[line.id] = tk.BooleanVar(value=False)
            check = ttk.Checkbutton(self.lines_row, text=line.name, variable=var,
                                    command=lambda lid=line.id: self._line_toggled(lid))
            check.pack(side="left", padx=(0, 14))
            self.line_checks[line.id] = check

    # lines and blocks

    def _line_toggled(self, line_id):
        if self.line_vars[line_id].get():
            self._add_block(line_id)
        else:
            self._remove_block(line_id)
        self.update_send_button()

    def _add_block(self, line_id, choice: LineChoice | None = None):
        data = self.services.data.current
        line = data.lines.get(line_id) if data is not None else None
        if line is None or line_id in self.blocks:
            return
        block = LineBlock(self.blocks_frame, self, line)
        self.blocks[line_id] = block
        choice = choice if choice is not None else self.kept.pop(line_id, None)
        if choice is not None:
            block.fill(choice)
        else:
            block.recompute()
        self._arrange_blocks()

    def _remove_block(self, line_id):
        block = self.blocks.pop(line_id, None)
        if block is None:
            return
        self.kept[line_id] = block.choice()
        _clear(block, self.texts)
        self.texts.forget(block)
        block.destroy()
        self._arrange_blocks()

    def _arrange_blocks(self):
        """Blocks in line order."""
        order = {ln.id: n for n, ln in enumerate(self._active_lines())}
        for block in self.blocks.values():
            block.pack_forget()
        for line_id in sorted(self.blocks, key=lambda lid: order.get(lid, len(order))):
            self.blocks[line_id].pack(fill="x", padx=4, pady=(0, 10))

    def _ordered_blocks(self):
        order = {ln.id: n for n, ln in enumerate(self._active_lines())}
        return [self.blocks[lid] for lid in sorted(self.blocks, key=lambda lid: order.get(lid, len(order)))]

    def _reset(self):
        for line_id in list(self.blocks):
            self._remove_block(line_id)
        self.kept.clear()
        for var in self.line_vars.values():
            var.set(False)
        self._notes = []

    def fill(self, request: SendRequest) -> None:
        """The request's choices, line by line; a line that isn't active any more is noted."""
        self._reset()
        data = self.services.data.current
        notes = []
        for choice in request.lines:
            line = data.lines.get(choice.line_id) if data is not None else None
            if line is None or not line.active:
                name = line.name if line is not None else choice.line_id
                notes.append(Msg(N_("{line} isn't active any more."), {"line": name}))
                continue
            if choice.line_id not in self.line_vars:
                self._build_line_checks()
            self.line_vars[choice.line_id].set(True)
            self._add_block(choice.line_id, copy.deepcopy(choice))
        self._notes = notes
        self._show_problems()
        self.update_send_button()

    def tick_lines(self, line_ids: list[str]) -> None:
        """Those lines ticked, with empty choices."""
        self._reset()
        for line_id in line_ids:
            if line_id in self.line_vars:
                self.line_vars[line_id].set(True)
                self._add_block(line_id)
        self._show_problems()
        self.update_send_button()

    def request(self) -> SendRequest:
        """The choices as a request with a new send_id."""
        identity = self.services.identity
        return SendRequest(send_id=new_id("sd"), sender=identity.user, computer=identity.computer,
                           created_at=self.services.clock.time(),
                           lines=[block.choice() for block in self._ordered_blocks()])

    def has_choices(self) -> bool:
        return any(block.has_product() for block in self.blocks.values())

    def view(self, machine_id):
        return _board_view(self.board, machine_id)

    # banners and buttons

    def _show_problems(self):
        data = self.services.data
        self.problems_label.show(list(data.problems) + list(self._notes))

    def _show_banners(self):
        data = self.services.data
        current = data.current
        if current is not None and current.source == "local":
            self.texts.set(self.local_label, N_("Using this PC's copy of the templates, as of {time}. The server "
                                                "can't be reached, so every machine's result will be amber."),
                           time=when(current.reached_at))
            self.local_banner.grid()
        else:
            self.local_banner.grid_remove()
        ok, msg = data.can_send()
        if ok:
            self.blocked_banner.grid_remove()
        else:
            self.texts.msg(self.blocked_label, msg)
            self.blocked_banner.grid()
        self._show_problems()
        self.update_send_button()

    def _retry(self):
        self.services.store.probe_now()
        self.services.data.reload()

    def _load_queue(self):
        services = self.services

        def done(n, error):
            if not _alive(self):
                return
            self._pending_logs = n if error is None and isinstance(n, int) else 0
            self._show_queue()

        services.bg.run(services.data.pending_logs, done)

    def _show_queue(self):
        n = self._pending_logs
        if n <= 0:
            self.queue_label.grid_remove()
            return
        if n == 1:
            self.texts.set(self.queue_label, N_("1 log entry waiting to upload."))
        else:
            self.texts.set(self.queue_label, N_("{n} log entries waiting to upload."), n=n)
        self.queue_label.grid()

    def update_send_button(self) -> None:
        if not hasattr(self, "send_button"):
            return
        n = sum(block.ticked() for block in self.blocks.values())
        if n == 1:
            self.texts.set(self.send_button, N_("Send to 1 machine"))
        else:
            self.texts.set(self.send_button, N_("Send to {n} machines"), n=n)
        _enable(self.send_button, self._can_send())

    def _can_send(self) -> bool:
        services = self.services
        if self.page != "choose" or not self.blocks:
            return False
        if not all(block.ready() for block in self.blocks.values()):
            return False
        return bool(services.data.can_send()[0]) and roles(services).sender

    # events

    def _on_data(self, event):
        if self.page == "choose":
            self._build_line_checks()
            for block in list(self.blocks.values()):
                block.data_changed()
        self._show_banners()
        self._load_queue()

    def _on_board(self, event):
        self.board = event.board
        if self.page != "choose":
            return
        for block in self.blocks.values():
            block.update_board()
            block.recompute()

    # sending

    def _send(self):
        if self.page != "choose":
            return
        for block in self.blocks.values():
            block.recompute()           # nothing typed in the last moment is missed
        if not self._can_send():
            return
        services = self.services
        data = services.data.current
        request = self.request()
        try:
            plan = hx_message.merge_plan(request, data, services.identity)
        except PlanError as e:
            self._notes = list(e.problems)
            self._show_problems()
            return
        except NotAllowed as e:
            hx_ui_common.tell(self, e.msg, kind="error")
            return
        self.start(plan)

    def start(self, plan) -> None:
        """Leave the choice page and run the plan: the countdown, then the Send (ui.md §5.7)."""
        services = self.services
        self.plan = plan
        self.page = "countdown"
        self.cancel_token = CancelToken()
        self._work_failed = False
        self._countdown_at = None
        self._notes = []
        self.choose_page.grid_remove()
        self.progress_page.grid()
        self._place_status(self.progress_page)
        self._set_status(None)
        self.work_problem_label.show([])
        self.progress_close_button.grid_remove()
        self.working_label.grid_remove()
        self.countdown_frame.grid()
        self.cancel_button.grid()
        _enable(self.cancel_button, True)
        line_names, origins = [], {}
        for mp in plan.machines:
            if mp.line.id not in origins:
                origins[mp.line.id] = (mp.line.name, mp.origin_line)
                line_names.append(mp.line.name)
        self.texts.set(self.progress_heading, N_("Sending to {lines}"), lines=_joined(line_names))
        _clear(self.origins_frame, self.texts)
        self.countdown_origins.clear()
        for row, (line_id, (name, origin)) in enumerate(origins.items()):
            ttk.Label(self.origins_frame, text=name, width=16).grid(row=row, column=0, sticky="w")
            shown = BagText(self.origins_frame, size=16, bold=True)
            shown.set(origin)
            shown.grid(row=row, column=1, sticky="w", pady=2)
            self.countdown_origins[line_id] = shown
        self.texts.set(self.countdown_caption, N_("Getting ready..."))
        self.countdown_label.configure(text=str(int(services.timings.countdown_s)))
        _clear(self.rows_scroller.inner, self.texts)
        self.progress_rows.clear()
        for mp in plan.machines:
            row = ProgressRow(self.rows_scroller.inner, self, mp.machine.name)
            row.pack(fill="x", padx=4)
            self.progress_rows[mp.machine.id] = row
        names = [mp.machine.name for mp in plan.machines]
        mark_running(f"send:{plan.send_id}", Msg(N_("A Send to {machines} is still running."),
                                                 {"machines": _joined(names)}))
        token = self.cancel_token
        start_thread(services, f"hx-send-{plan.send_id}", plan.send_id,
                     lambda: services.pipeline().run(plan, services.bus.post, cancel=token))
        self.update_send_button()

    def _cancel(self):
        if self.page != "countdown" or self.cancel_token is None:
            return
        self.cancel_token.cancel()
        _enable(self.cancel_button, False)
        self.texts.set(self.countdown_caption, N_("Cancelling..."))
        self._stop_countdown()

    def _stop_countdown(self):
        if self._countdown_timer is not None:
            try:
                self.after_cancel(self._countdown_timer)
            except tk.TclError:
                pass
            self._countdown_timer = None

    def _tick(self):
        self._countdown_timer = None
        if self.page != "countdown" or self._countdown_at is None:
            return
        timings = self.services.timings
        shown = max(0, math.ceil(timings.countdown_s - (self.services.clock.time() - self._countdown_at)))
        self.countdown_label.configure(text=str(shown))
        if shown <= 0:
            self._go_working()          # fallback: the count is over
            return
        self._countdown_timer = self.later(COUNTDOWN_MS, self._tick)

    def _go_working(self):
        """The pipeline's "go": Cancel is gone for good and "Hang on" shows."""
        if self.page != "countdown":
            return
        self.page = "working"
        self._stop_countdown()
        self.cancel_button.grid_remove()
        _enable(self.cancel_button, False)
        self.countdown_frame.grid_remove()
        self.working_label.grid()

    def _on_send_event(self, event):
        plan = self.plan
        if plan is None or event.send_id != plan.send_id:
            return
        if not event.machine_id:
            if event.step == STEP_LOCK and event.status == "running" and self.page == "countdown":
                self._countdown_at = event.at
                if self.cancel_token is None or not self.cancel_token.cancelled:
                    self.texts.set(self.countdown_caption, N_("Sending in"))
                    self._stop_countdown()
                    self._tick()
            elif event.step == STEP_LOCK and event.status == "ok":
                self._go_working()
            else:
                self._show_send_event(event)
            return
        if event.step in STEPS and STEPS.index(event.step) >= STEPS.index(STEP_CHECK_MACHINE):
            self._go_working()          # fallback: a printer step has started
        row = self.progress_rows.get(event.machine_id)
        if row is not None:
            row.show_event(event)

    def _show_send_event(self, event):
        mark, style = MARKS.get(event.status, MARKS["running"])
        step = hx_message.STEP_TEXTS.get(event.step, Msg(event.step))
        msg = event.msg if event.msg is not None else step
        self._set_status(msg, style)

    def _on_finished(self, event):
        plan = self.plan
        result = event.result
        if plan is None or result.send_id != plan.send_id:
            return
        services = self.services
        mark_done(f"send:{plan.send_id}")
        self._stop_countdown()
        if hx_send.is_cancelled(result):
            self._back_to_choices()
            self._place_status(self.choose_page)
            self._set_status(hx_message.NOTE_TEXTS["cancelled"], "Ok.TLabel")
            return
        nav = services.nav
        if any(m.ok for m in result.machines):
            window = ResultWindow(self.master, services, result)
            window.request = plan.request
        if any(not m.ok for m in result.machines):
            nav.open_failure(result, plan)
        if any("no_lock" in m.notes for m in result.machines):
            nav.note_offline_send(result.send_id)
        self.destroy()

    def _on_work_failed(self, event):
        plan = self.plan
        if plan is None or event.ref != plan.send_id:
            return
        mark_done(f"send:{plan.send_id}")
        if self.page == "countdown":
            # Nothing was sent: the pipeline refused before any printer request
            self._back_to_choices()
            self._notes = [event.msg]
            self._show_problems()
            return
        self._work_failed = True
        self.work_problem_label.show([Msg(N_("Stripes hit a problem during the Send. Check each machine's card on "
                                             "the MapleJet tab.")), event.msg])
        self.progress_close_button.grid()

    def _back_to_choices(self):
        self.page = "choose"
        self.plan = None
        self.cancel_token = None
        self._countdown_at = None
        self.progress_page.grid_remove()
        self.choose_page.grid()
        self._place_status(self.choose_page)
        self._set_status(None)
        self._show_banners()
        self._load_queue()
        for block in self.blocks.values():
            block.recompute()
        self.update_send_button()

    # language and closing

    def refresh_texts(self) -> None:
        _render_lines(self)

    def close(self) -> None:
        if self.page == "countdown":
            hx_ui_common.tell(self, Msg(N_("Press Cancel to stop the Send.")))
            return
        if self.page == "working" and not self._work_failed:
            hx_ui_common.tell(self, Msg(N_("The Send is running. Wait for it to finish.")))
            return
        if self.page == "choose" and self.has_choices():
            if not hx_ui_common.ask(self, Msg(N_("Close the Send window? Your choices will be lost.")),
                                    yes=N_("Close"), no=N_("Keep it open")):
                return
        self.destroy()

    def destroy(self) -> None:
        self._stop_countdown()
        super().destroy()


def run_send(master, services, plan) -> SendWindow | None:
    """Try again: the plan run through the Navigator's one Send window; None when it couldn't start."""
    window = services.nav.window("send")
    if window is None:
        window = SendWindow(master, services, prefill=plan.request)
        window.start(plan)
        return window
    if window.page != "choose":
        window.show()
        hx_ui_common.tell(window, Msg(N_("A Send is running. Wait for it to finish.")))
        return None
    if not hx_ui_common.ask(window, Msg(N_("Replace the choices in the Send window?")), yes=N_("Replace them"),
                            no=N_("Keep them")):
        return None
    window.fill(plan.request)
    window.start(plan)
    window.show()
    return window


# --------------------------------------------------------------------------
# The result and confirmation window
# --------------------------------------------------------------------------

class ResultWindow(HxWindow):
    """Check the new messages: each live machine's fresh picture and origin, confirmed line by line by
    the person who sent it (ui.md §6)."""

    def __init__(self, master, services, result: SendResult):
        self.key = f"result:{result.send_id}"
        super().__init__(master, services, title=N_("Check the new messages"))
        self.geometry("1000x780")
        self.minsize(760, 560)
        self.result = result
        self.request: SendRequest | None = None
        self.pending: list[str] = []
        self.line_state: dict[str, str] = {}
        self.pictures: dict[str, PictureView] = {}
        self.origins: dict[str, BagText] = {}
        self.right_buttons: dict[str, ttk.Button] = {}
        self.wrong_buttons: dict[str, ttk.Button] = {}
        self.line_frames: dict[str, ttk.LabelFrame] = {}
        self.state_labels: dict[str, ttk.Label] = {}
        self._line_parts: dict[str, dict] = {}
        self._sender = result.sender
        self._no_entry = False
        self._told_pending = False
        self._build()
        self.subscribe(BoardUpdate, lambda e: self._show_headlines())
        self.fill(result)

    def _build(self):
        texts = self.texts
        body = ttk.Frame(self, padding=12)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(5, weight=1)
        heading = ttk.Label(body, style="Hx.Head.TLabel")
        texts.set(heading, N_("Check the new messages"))
        heading.grid(row=0, column=0, sticky="w")
        self.sent_label = ttk.Label(body)
        self.sent_label.grid(row=1, column=0, sticky="w")
        self.instructions_label = ttk.Label(body)
        texts.set(self.instructions_label, N_("Look at each printer's picture. Wait for the first bags, then confirm "
                                              "each line."))
        self.instructions_label.grid(row=2, column=0, sticky="w", pady=(0, 6))
        self.notes_frame = ttk.Frame(body)
        self.notes_frame.grid(row=3, column=0, sticky="ew")
        self.status_label = _Lines(body, style="TLabel")
        self.status_label.grid(row=4, column=0, sticky="w", pady=(4, 4))
        self.status_label.show([])
        self.scroller = Scroller(body)
        self.scroller.grid(row=5, column=0, sticky="nsew")
        self.close_button = ttk.Button(body, command=self.close)
        texts.set(self.close_button, N_("Close"))
        self.close_button.grid(row=6, column=0, sticky="e", pady=(8, 0))

    def _is_sender(self) -> bool:
        return bool(self._sender) and same_user(self._sender, self.services.identity.user)

    def fill(self, result: SendResult, *, request: SendRequest | None = None,
             pending: list[str] | None = None) -> None:
        """Show a result: every live machine with its picture and origin, grouped by line; the lines
        with pending machines get the buttons when this person sent it."""
        self.result = result
        if request is not None:
            self.request = request
        if not self._no_entry:
            self._sender = result.sender
        ok_ids = [m.machine_id for m in result.machines if m.ok]
        self.pending = [mid for mid in (pending if pending is not None else ok_ids) if mid in ok_ids]
        texts = self.texts
        if result.sender:
            texts.set(self.sent_label, N_("Sent by {user} on {computer} at {time}."), user=result.sender,
                      computer=result.computer, time=when(result.finished_at))
        _clear(self.notes_frame, texts)
        notes = []
        if self._no_entry:
            notes.append(Msg(N_("This PC can't find the full record of this Send. These are the printers' latest "
                                "pictures.")))
        if result.from_local_copy:
            notes.append(Msg(N_("Sent from this PC's copy of the templates (as of {time}). Check the first bags on "
                                "each machine."), {"time": when(result.data_as_of)}))
        if result.time_source == "pc":
            notes.append(Msg(N_("The time server didn't answer, so the PC's time was used.")))
        if result.log_queued:
            notes.append(Msg(N_("The send log is waiting on this PC. It uploads when the server is back.")))
        for note in notes:
            label = ttk.Label(self.notes_frame, style="Warn.TLabel", wraplength=_WRAP, justify="left")
            texts.msg(label, note)
            label.pack(anchor="w")
        self._show_lines()

    def _show_lines(self):
        texts = self.texts
        inner = self.scroller.inner
        _clear(inner, texts)
        for d in (self.pictures, self.origins, self.right_buttons, self.wrong_buttons, self.line_frames,
                  self.state_labels, self._line_parts):
            d.clear()
        self.line_state = {}
        result = self.result
        data = self.services.data.current
        groups: dict[str, list[MachineResult]] = {}
        for m in result.machines:
            machine = data.machines.get(m.machine_id) if data is not None else None
            groups.setdefault(machine.line_id if machine is not None else "", []).append(m)

        def line_name(line_id):
            line = data.lines.get(line_id) if data is not None else None
            return line.name if line is not None else ""

        for line_id in sorted(groups, key=lambda lid: (hx_state.natural_key(line_name(lid)), lid)):
            self._show_line(inner, line_id, line_name(line_id), groups[line_id])
        self.status_label.configure(style="TLabel")
        if not self.pending:
            self.status_label.show([Msg(N_("Nothing in this Send is waiting for confirmation."))])
        else:
            self.status_label.show([])

    def _show_line(self, inner, line_id, name, results):
        texts = self.texts
        frame = ttk.LabelFrame(inner, text=name, padding=10)      # a record name: data
        frame.pack(fill="x", padx=4, pady=(0, 10))
        self.line_frames[line_id] = frame
        pending = [m.machine_id for m in results if m.machine_id in self.pending]
        self.line_state[line_id] = "pending" if pending else "none"
        names = dict(zip([m.machine_id for m in results], _machine_names(self.services,
                                                                        [m.machine_id for m in results])))
        for m in results:
            if not m.ok:
                failed = ttk.Label(frame, style="Bad.TLabel")
                texts.set(failed, N_("{machine}: the Send failed. See the red window."), machine=names[m.machine_id])
                failed.pack(anchor="w", pady=(4, 4))
                continue
            self._show_machine(frame, m, names[m.machine_id])
        foot = ttk.Frame(frame)
        foot.pack(fill="x", pady=(6, 0))
        parts = {"foot": foot, "pending": pending}
        self._line_parts[line_id] = parts
        self._show_line_foot(line_id)

    def _show_machine(self, frame, m, name):
        texts = self.texts
        box = ttk.Frame(frame, padding=(0, 6))
        box.pack(fill="x", anchor="w")
        ttk.Label(box, text=name, style="Hx.Section.TLabel").pack(anchor="w")
        state = ttk.Label(box)
        state.pack(anchor="w")
        self.state_labels[m.machine_id] = state
        for note in m.notes:
            if note in hx_message.NOTE_TEXTS:
                label = ttk.Label(box, style="Warn.TLabel", wraplength=_WRAP, justify="left")
                texts.msg(label, hx_message.NOTE_TEXTS[note])
                label.pack(anchor="w")
        picture = PictureView(box, width=640)
        picture.set_bmp(m.readbacks[-1].preview_bmp if m.readbacks else None)
        picture.pack(anchor="w", pady=(4, 4))
        self.pictures[m.machine_id] = picture
        origin_row = ttk.Frame(box)
        origin_row.pack(anchor="w")
        origin = BagText(origin_row, size=22, bold=True, wrap_px=900)
        origin.set(m.origin_line)
        origin.pack(side="left")
        self.origins[m.machine_id] = origin
        if not m.origin_line:
            unknown = ttk.Label(origin_row, style="Muted.TLabel")
            texts.set(unknown, N_("(origin unknown)"))
            unknown.pack(side="left", padx=(6, 0))
        if not self._no_entry:
            before = m.previous.origin_line if m.previous is not None else ""
            change = hx_message.origin_change_text(before, m.origin_line)
            change_row = ttk.Frame(box)
            change_row.pack(anchor="w", pady=(2, 0))
            if change:
                caption = ttk.Label(change_row)
                texts.set(caption, N_("Before:"))
                caption.pack(side="left")
                if not before:
                    unknown = ttk.Label(change_row, style="Muted.TLabel")
                    texts.set(unknown, N_("(origin unknown) →"))
                    unknown.pack(side="left", padx=(4, 0))
                shown = BagText(change_row, size=11, bold=True, background=HIGHLIGHT_BG)
                shown.set(change)
                shown.pack(side="left", padx=(4, 0))
            else:
                same = ttk.Label(change_row, style="Muted.TLabel")
                texts.set(same, N_("Same origin as before."))
                same.pack(side="left")
        if m.message_name:
            message = ttk.Label(box, style="Hx.Small.TLabel")
            texts.set(message, N_("Message {name} in slot {slot}."), name=m.message_name,
                      slot=m.slot if m.slot is not None else "?")
            message.pack(anchor="w")
        self._show_state(m.machine_id)

    def _show_state(self, mid):
        """Pending: waiting for the sender; otherwise the machine's headline from the board."""
        label = self.state_labels.get(mid)
        if label is None:
            return
        if mid in self.pending:
            self.texts.set(label, N_("Live and checked twice. Waiting for you to confirm."))
            return
        view = _board_view(self.services.watcher.latest, mid)
        if view is not None:
            self.texts.msg(label, view.headline)
        else:
            self.texts.forget(label)
            label.configure(text="")

    def _show_headlines(self):
        for mid in self.state_labels:
            if mid not in self.pending:
                self._show_state(mid)

    def _show_line_foot(self, line_id):
        """The line's buttons, or what happened to it."""
        parts = self._line_parts[line_id]
        foot = parts["foot"]
        texts = self.texts
        _clear(foot, texts)
        self.right_buttons.pop(line_id, None)
        self.wrong_buttons.pop(line_id, None)
        state = self.line_state.get(line_id, "none")
        parts["error"] = _Lines(foot, style="Bad.TLabel")
        parts["error"].grid(row=0, column=0, columnspan=3, sticky="w")
        parts["error"].show(parts.get("error_msgs", []))
        if state in ("pending", "saving"):
            if not self._is_sender():
                only = ttk.Label(foot, style="Muted.TLabel")
                texts.set(only, N_("Only {user} can confirm this Send."), user=self._sender or "?")
                only.grid(row=1, column=0, sticky="w")
                return
            foot.columnconfigure(0, weight=1)
            wrong = ttk.Button(foot, command=lambda: self._answer(line_id, False))
            texts.set(wrong, N_("Something's wrong"))
            wrong.grid(row=1, column=1, padx=(0, 8))
            right = ttk.Button(foot, style="Hx.Big.TButton", command=lambda: self._answer(line_id, True))
            texts.set(right, N_("Everything is right"))
            right.grid(row=1, column=2)
            self.wrong_buttons[line_id] = wrong
            self.right_buttons[line_id] = right
            if state == "saving":
                _enable(wrong, False)
                _enable(right, False)
        elif state == "confirmed":
            done = ttk.Label(foot, style="Ok.TLabel")
            texts.set(done, N_("Confirmed by you at {time}."), time=parts.get("confirmed_at", ""))
            done.grid(row=1, column=0, sticky="w")
        elif state in ("rejected", "stopped"):
            said = ttk.Label(foot, style="Bad.TLabel", wraplength=600, justify="left")
            if state == "rejected":
                texts.set(said, N_("You said something's wrong. These machines stay red until a new Send."))
            else:
                texts.set(said, N_("Printing stopped. This line needs a new Send."))
            said.grid(row=1, column=0, sticky="w")
            foot.columnconfigure(0, weight=1)
            if state == "rejected":
                stop = ttk.Button(foot, command=lambda: self._open_stop(line_id))
                texts.set(stop, N_("Stop printing..."))
                stop.grid(row=1, column=1, padx=(8, 8))
            fix = ttk.Button(foot, style="Hx.Main.TButton", command=lambda: self._fix(line_id))
            texts.set(fix, N_("Fix and send again"))
            fix.grid(row=1, column=2)

    # confirming

    def _answer(self, line_id, everything_right):
        if self.line_state.get(line_id) != "pending":
            return
        parts = self._line_parts[line_id]
        ids = [mid for mid in parts["pending"] if mid in self.pending]
        if not ids:
            return
        services = self.services
        send_id = self.result.send_id
        self.line_state[line_id] = "saving"
        parts["error_msgs"] = []
        self._show_line_foot(line_id)

        def work():
            services.pipeline().confirm(send_id, ids, everything_right)

        def done(_result, error):
            if not _alive(self):
                return
            if error is not None:
                self.line_state[line_id] = "pending"
                parts["error_msgs"] = [_error_msg(error)]
                if isinstance(error, ShareError) and any("no_lock" in m.notes for m in self.result.machines):
                    services.nav.note_offline_answer(send_id, ids, everything_right)
                self._show_line_foot(line_id)
                return
            self.pending = [mid for mid in self.pending if mid not in ids]
            if everything_right:
                self.line_state[line_id] = "confirmed"
                parts["confirmed_at"] = when(services.clock.time())
                self._show_line_foot(line_id)
            else:
                self.line_state[line_id] = "rejected"
                parts["rejected_ids"] = ids
                self._show_line_foot(line_id)
                self._open_stop(line_id)
            for mid in ids:
                self._show_state(mid)

        services.bg.run(work, done)

    def _open_stop(self, line_id):
        """Something's wrong goes on to the Stop question at once (send.md §13.1)."""
        parts = self._line_parts.get(line_id, {})
        ids = parts.get("rejected_ids") or parts.get("pending") or []
        if not ids:
            return
        key = "stop:" + ",".join(sorted(ids))
        window = self.services.nav.window(key)
        if window is not None:
            window.show()
            return
        StopDialog(self.master, self.services, machine_ids=list(ids), reason="rejected",
                   send_id=self.result.send_id, request=self.request)

    def _fix(self, line_id):
        """Fix and send again: the Send window with this line's choices filled in."""
        services = self.services
        send_id = self.result.send_id

        def open_with(request):
            choices = [c for c in (request.lines if request is not None else []) if c.line_id == line_id]
            if not choices:
                services.nav.open_send(line_ids=[line_id])
                return
            identity = services.identity
            services.nav.open_send(prefill=SendRequest(send_id="", sender=identity.user, computer=identity.computer,
                                                       created_at=services.clock.time(),
                                                       lines=[copy.deepcopy(choices[0])]))

        if self.request is not None:
            open_with(self.request)
            return

        def done(entry, error):
            if not _alive(self):
                return
            if error is None and entry is not None:
                try:
                    self.request = hx_send.request_from_log(entry)
                except (KeyError, ValueError):
                    self.request = None
            open_with(self.request)

        services.bg.run(lambda: services.data.find_send(send_id), done)

    def stopped(self, line_id: str) -> None:
        """The Stop dialog stopped this line's machines."""
        if line_id in self._line_parts:
            self.line_state[line_id] = "stopped"
            self._show_line_foot(line_id)

    # reopening from a card

    @staticmethod
    def from_log(master, services, send_id: str) -> "ResultWindow | None":
        """The result window again, filled from the send log (or, without it, from the expected states);
        None when one for that send_id was already open (it is shown instead)."""
        window = services.nav.window(f"result:{send_id}")
        if window is not None:
            window.show()
            return None
        empty = SendResult(send_id=send_id, sender="", computer="", started_at=0.0, finished_at=0.0, machines=[],
                           time_source="server")
        window = ResultWindow(master, services, empty)
        window.status_label.configure(style="TLabel")
        window.status_label.show([Msg(N_("Loading the Send..."))])

        def work():
            entry = services.data.find_send(send_id)
            pending = services.pipeline().pending_confirmation(send_id)
            states, pictures = {}, {}
            if entry is None:
                for mid in pending:
                    try:
                        states[mid] = services.store.read_expected(mid)
                    except ShareError:
                        states[mid] = None
                    try:
                        pictures[mid] = services.store.read_preview(mid)
                    except ShareError:
                        pictures[mid] = None
            return entry, pending, states, pictures

        def done(found, error):
            if not _alive(window):
                return
            if error is not None:
                window.status_label.configure(style="Bad.TLabel")
                window.status_label.show([_error_msg(error)])
                return
            entry, pending, states, pictures = found
            if entry is not None:
                try:
                    result = hx_send.result_from_log(entry)
                    request = hx_send.request_from_log(entry)
                except (KeyError, ValueError) as e:
                    window.status_label.configure(style="Bad.TLabel")
                    window.status_label.show([_error_msg(e)])
                    return
                window.fill(result, request=request, pending=pending)
            else:
                window._fill_without_entry(send_id, pending, states, pictures)

        services.bg.run(work, done)
        return window

    def _fill_without_entry(self, send_id, pending, states, pictures):
        """The pending machines from their expected states, with the printers' latest pictures."""
        board = self.services.watcher.latest
        machines, sender, computer, at = [], "", "", 0.0
        for mid in pending:
            e = states.get(mid)
            view = _board_view(board, mid)
            origin = (e.origin_line if e is not None else "") or (view.origin_line if view is not None else "")
            name = (e.message_name if e is not None else "") or (view.message_name if view is not None else "")
            slot = e.slot if e is not None else None
            design = MessageDesign(slot=slot or 0, name=name, empty=False, preview_bmp=pictures.get(mid) or b"")
            machines.append(MachineResult(machine_id=mid, colour=AMBER, ok=True, step=STEP_CONFIRM_2, slot=slot,
                                          message_name=name, origin_line=origin, readbacks=[design],
                                          notes=list(e.notes) if e is not None else []))
            who = (e.sender if e is not None else "") or (view.confirm_pending_by if view is not None else "")
            if who and not sender:
                sender = who
                computer = e.computer if e is not None else ""
                at = e.written_at if e is not None else 0.0
        self._no_entry = True
        self._sender = sender
        result = SendResult(send_id=send_id, sender=sender, computer=computer, started_at=at, finished_at=at,
                            machines=machines, time_source="server")
        self.fill(result, pending=list(pending))

    # language and closing

    def refresh_texts(self) -> None:
        _render_lines(self)

    def close(self) -> None:
        if "saving" in self.line_state.values():
            hx_ui_common.tell(self, Msg(N_("Wait until the confirmation is saved.")))
            return
        if self._is_sender() and "pending" in self.line_state.values() and not self._told_pending:
            self._told_pending = True
            hx_ui_common.tell(self, Msg(N_("Lines you haven't confirmed stay amber \"Send not confirmed\". You can "
                                           "confirm them later from the machine's card on the MapleJet tab.")))
        self.destroy()


# --------------------------------------------------------------------------
# The red failure window
# --------------------------------------------------------------------------

class FailureWindow(HxWindow):
    """Send failed: full screen and on top, until Try again or Leave it (ui.md §7)."""

    def __init__(self, master, services, result: SendResult, plan):
        self.key = f"failure:{result.send_id}"
        super().__init__(master, services, title=N_("Send failed"), background=RED_BG)
        self.result = result
        self.plan = plan
        self.rows = hx_send.failure_rows(result, plan)
        self.pictures: dict[str, PictureView] = {}
        self.origins: dict[str, BagText] = {}
        self.retry_send_id = ""
        self.passed_label = None
        self.try_button = None
        self.leave_button = None
        self._build()
        _full_screen(self)
        self.subscribe(SendEvent, self._on_retry_event)
        self.subscribe(SendFinished, self._on_retry_finished)
        self.subscribe(WorkFailed, self._on_retry_failed)
        self.show()

    def _build(self):
        texts = self.texts
        services = self.services
        result = self.result
        self.scroller = Scroller(self, background=RED_BG)
        self.scroller.pack(fill="both", expand=True)
        inner = self.scroller.inner
        pad = {"padx": 32}
        heading = _red_label(inner, 28, True)
        texts.set(heading, N_("Send failed"))
        heading.pack(anchor="w", pady=(24, 0), **pad)
        sent = _red_label(inner, 14)
        texts.set(sent, N_("Sent by {user} at {time}. Stripes stopped at the step that failed and did nothing else."),
                  user=result.sender, time=when(result.finished_at))
        sent.pack(anchor="w", pady=(4, 12), **pad)
        for row in self.rows:
            self._show_row(inner, row)
        passed = [m.machine_id for m in result.machines if m.ok]
        if passed:
            self.passed_label = _red_label(inner, 14)
            texts.set(self.passed_label, N_("{machines} went live. Confirm them in the result window."),
                      machines=_joined(_machine_names(services, passed)))
            self.passed_label.pack(anchor="w", pady=(4, 4), **pad)
        self.message_label = _red_label(inner, 14, True)
        self.message_label.pack(anchor="w", pady=(8, 0), **pad)
        buttons = tk.Frame(inner, background=RED_BG)
        buttons.pack(fill="x", pady=(12, 32), **pad)
        identity = services.identity
        if same_user(result.sender, identity.user) and roles(services).sender:
            self.leave_button = ttk.Button(buttons, style="Hx.Big.TButton", command=self._leave)
            texts.set(self.leave_button, N_("Leave it"))
            self.leave_button.pack(side="right")
            self.try_button = ttk.Button(buttons, style="Hx.Big.TButton", command=self._try_again)
            texts.set(self.try_button, N_("Try again"))
            self.try_button.pack(side="right", padx=(0, 12))
        else:
            only = _red_label(buttons, 14)
            texts.set(only, N_("Only {user} can choose Try again or Leave it."), user=result.sender)
            only.pack(side="right")

    def _show_row(self, inner, row):
        texts = self.texts
        block = tk.Frame(inner, background=RED_BG, highlightbackground=RED_FG, highlightcolor=RED_FG,
                         highlightthickness=2, padx=16, pady=12)
        block.pack(fill="x", padx=32, pady=(0, 12))
        title = _red_label(block, 20, True)
        title.configure(text=f"{row.machine_name} · {row.line_name}" if row.line_name else row.machine_name)
        title.pack(anchor="w")
        step = _red_label(block, 16)
        texts.set(step, N_("Failed at: {step}"), step=row.step_text)
        step.pack(anchor="w")
        if row.msg is not None:
            msg = _red_label(block, 14)
            texts.msg(msg, row.msg)
            msg.pack(anchor="w")
        if row.after_switch and row.after_switch_text is not None:
            after = _red_label(block, 20, True)
            texts.msg(after, row.after_switch_text)
            after.pack(anchor="w", pady=(4, 4))
        if row.differences:
            columns = ("where", "what", "sent", "read")
            tree = ttk.Treeview(block, columns=columns, show="headings", height=min(6, len(row.differences)))
            for column, english in zip(columns, (N_("Where"), N_("What"), N_("Sent"), N_("Read back"))):
                texts.heading(tree, column, english)
                tree.column(column, width=260 if column in ("sent", "read") else 140, stretch=True)
            for d in row.differences:
                tree.insert("", "end", values=(d.where, d.field, d.sent, d.read))   # data and bag text
            tree.pack(fill="x", pady=(6, 6))
        previous = _red_label(block, 14)
        if not row.previous_name:
            texts.set(previous, N_("Stripes doesn't know what this machine was last confirmed printing."))
        elif row.previous_confirmed:
            texts.set(previous, N_("Last confirmed printing: {name}"), name=row.previous_name)
        else:
            texts.set(previous, N_("Printing before this Send (not confirmed): {name}"), name=row.previous_name)
        previous.pack(anchor="w", pady=(6, 2))
        picture = PictureView(block, width=768, background=RED_BG, highlightthickness=2,
                              highlightbackground=RED_FG)
        picture.set_bmp(row.previous_preview or None)
        picture.pack(anchor="w", pady=(2, 4))
        self.pictures[row.machine_id] = picture
        origin_row = tk.Frame(block, background=RED_BG)
        origin_row.pack(anchor="w")
        origin = BagText(origin_row, size=24, bold=True, foreground=RED_FG, background=RED_BG)
        origin.set(row.previous_origin)
        origin.pack(side="left")
        self.origins[row.machine_id] = origin
        if not row.previous_origin:
            unknown = _red_label(origin_row, 18)
            texts.set(unknown, N_("(origin unknown)"))
            unknown.pack(side="left")

    # Try again and Leave it

    def _below(self, ask):
        """Run a question or another window's start with this window not on top, so the dialog it
        may show isn't hidden behind it; then on top again."""
        self.attributes("-topmost", False)
        try:
            return ask()
        finally:
            if _alive(self) and self.state() != "withdrawn":
                _full_screen(self)

    def _buttons(self, on):
        for button in (self.try_button, self.leave_button):
            if button is not None:
                _enable(button, on)

    def _try_again(self):
        """A fresh Send of the failed machines, through the one Send window; this window waits,
        withdrawn, until the retry is past its countdown."""
        ids = [row.machine_id for row in self.rows]
        if not ids:
            return
        new_plan = self.plan.restricted(ids)
        window = self._below(lambda: run_send(self.master, self.services, new_plan))
        if window is None:
            return
        self.retry_send_id = new_plan.send_id
        self.texts.forget(self.message_label)
        self.message_label.configure(text="")
        self.withdraw()

    def _on_retry_event(self, event):
        if (self.retry_send_id and event.send_id == self.retry_send_id and not event.machine_id
                and event.step == STEP_LOCK and event.status == "ok"):
            self.destroy()              # the retry's own windows take over

    def _on_retry_finished(self, event):
        if not self.retry_send_id or event.result.send_id != self.retry_send_id:
            return
        if hx_send.is_cancelled(event.result):
            self._come_back(Msg(N_("Try again was cancelled.")))
        else:
            self.destroy()

    def _on_retry_failed(self, event):
        if self.retry_send_id and event.ref == self.retry_send_id:
            self._come_back(event.msg)

    def _come_back(self, msg):
        """The retry stopped before any printer request: the red window again, waiting as before."""
        self.retry_send_id = ""
        self.texts.msg(self.message_label, msg)
        self.deiconify()
        _full_screen(self)
        self.show()

    def _leave(self):
        services = self.services
        ids = [row.machine_id for row in self.rows]
        question = Msg(N_("Leave {machines} red? The line stays red until a Send succeeds, the line's notify user is "
                          "alerted, and this is logged."), {"machines": _joined(_machine_names(services, ids))})
        if not self._below(lambda: hx_ui_common.ask(self, question, yes=N_("Leave it"), no=N_("Go back"))):
            return
        self._buttons(False)
        send_id = self.result.send_id

        def done(_result, error):
            if not _alive(self):
                return
            if error is None:
                self.destroy()
                return
            self.texts.msg(self.message_label, _error_msg(error))
            self._buttons(True)

        services.bg.run(lambda: services.pipeline().leave_it(send_id, ids), done)

    def close(self) -> None:
        self._below(lambda: hx_ui_common.tell(self, Msg(N_("Choose Try again or Leave it.")), kind="warning"))


# --------------------------------------------------------------------------
# Stop, Resume and Set clocks
# --------------------------------------------------------------------------

class _ActionDialog(HxWindow):
    """What the three dialogs share: the action's id, its thread and its events (ui.md §8)."""
    kind = ""
    running_text = ""

    def __init__(self, master, services, *, title, machine_ids, size="640x420"):
        super().__init__(master, services, title=title)
        self.geometry(size)
        self.minsize(480, 320)
        self.machine_ids = list(machine_ids)
        self.action_id = new_id("ac")
        self.state = ""
        self.rows: dict[str, ttk.Frame] = {}
        self._running_key = ""
        self.subscribe(ActionEvent, self._on_action_event)
        self.subscribe(ActionFinished, self._on_action_finished)
        self.subscribe(WorkFailed, self._on_work_failed)

    def names(self, machine_ids=None):
        return _machine_names(self.services, self.machine_ids if machine_ids is None else machine_ids)

    def run_action(self, call, machine_ids):
        """call(actions, machine_ids, on_event, action_id) on the hx-action-<action_id> thread."""
        services = self.services
        action_id = self.action_id
        self._running_key = f"action:{action_id}"
        mark_running(self._running_key, Msg(self.running_text, {"machines": _joined(self.names(machine_ids))}))
        ids = list(machine_ids)
        start_thread(services, f"hx-action-{action_id}", action_id,
                     lambda: call(services.actions(), ids, services.bus.post, action_id))

    def _done_running(self):
        if self._running_key:
            mark_done(self._running_key)
            self._running_key = ""

    def _on_action_event(self, event):
        if event.action_id == self.action_id:
            self.action_event(event)

    def _on_action_finished(self, event):
        if event.result.action_id == self.action_id:
            self._done_running()
            self.action_finished(event.result)

    def _on_work_failed(self, event):
        if event.ref == self.action_id:
            self._done_running()
            self.work_failed(event.msg)

    def action_event(self, event):
        row = self.rows.get(event.machine_id)
        if row is None:
            return
        mark, style = MARKS.get(event.status, MARKS["running"])
        row.mark_label.configure(text=mark, style=style)
        self.texts.msg(row.msg_label, event.msg)
        row.msg_label.configure(style=style if event.msg is not None else "Muted.TLabel")

    def action_finished(self, result):
        pass

    def work_failed(self, msg):
        pass

    def _make_rows(self, master, machine_ids):
        _clear(master, self.texts)
        self.rows.clear()
        for mid, name in zip(machine_ids, self.names(machine_ids)):
            row = ttk.Frame(master, padding=(0, 2))
            row.pack(fill="x", anchor="w")
            ttk.Label(row, text=name, width=24).pack(side="left", anchor="n")
            row.mark_label = ttk.Label(row, text=MARKS["running"][0], style=MARKS["running"][1], width=2)
            row.mark_label.pack(side="left", anchor="n")
            row.msg_label = ttk.Label(row, style="Muted.TLabel", wraplength=420, justify="left")
            row.msg_label.pack(side="left", anchor="n")
            self.rows[mid] = row

    def refresh_texts(self) -> None:
        _render_lines(self)

    def destroy(self) -> None:
        # The action itself carries on; can_quit only asks while it is running
        super().destroy()


class StopDialog(_ActionDialog):
    """Stop printing on machines, after a rejection or because the sender asked (ui.md §8.1)."""
    kind = "stop"
    running_text = N_("Stripes is stopping printing on {machines}.")

    def __init__(self, master, services, *, machine_ids: list[str], reason: str = "user", send_id: str = "",
                 request: SendRequest | None = None):
        self.key = "stop:" + ",".join(sorted(machine_ids))
        super().__init__(master, services, title=N_("Stop printing"), machine_ids=machine_ids)
        self.reason = reason
        self.send_id = send_id
        self.request = request
        self.state = "ask"
        self.fix_button = None
        self.close_button = None
        self.try_button = None
        self._failed_ids: list[str] = []
        self._normal_bg = self.cget("background")
        self._build()

    def _build(self):
        texts = self.texts
        self.page = ttk.Frame(self, padding=14)
        self.page.pack(fill="both", expand=True)
        self.page.columnconfigure(0, weight=1)
        self.question_label = ttk.Label(self.page, style="Hx.Head.TLabel", wraplength=600, justify="left")
        texts.set(self.question_label, N_("Stop printing on {machines}?"), machines=_joined(self.names()))
        self.question_label.grid(row=0, column=0, sticky="w")
        self.explain_label = ttk.Label(self.page, wraplength=600, justify="left")
        if self.reason == "rejected":
            texts.set(self.explain_label, N_("You said something's wrong with the new message. Printing is turned off "
                                             "and read back twice. Resume stays blocked for these machines, so the "
                                             "rejected message can't be restarted by accident."))
        else:
            texts.set(self.explain_label, N_("Printing is turned off and read back twice to make sure it's off."))
        self.explain_label.grid(row=1, column=0, sticky="w", pady=(8, 8))
        self.rows_frame = ttk.Frame(self.page)
        self.rows_frame.grid(row=2, column=0, sticky="nsew")
        self.result_label = _Lines(self.page, style="TLabel", wrap=600)
        self.result_label.grid(row=3, column=0, sticky="w", pady=(8, 0))
        self.result_label.show([])
        self.buttons = ttk.Frame(self.page)
        self.buttons.grid(row=4, column=0, sticky="ew", pady=(12, 0))
        self.stop_button = ttk.Button(self.buttons, style="Hx.Main.TButton", command=self._stop)
        texts.set(self.stop_button, N_("Stop printing"))
        self.stop_button.pack(side="right")
        self.back_button = ttk.Button(self.buttons, command=self.destroy)
        texts.set(self.back_button, N_("Go back"))
        self.back_button.pack(side="right", padx=(0, 8))
        self.page.rowconfigure(2, weight=1)

        # The red page, when stopping fails (built when needed)
        self.red_page = None

    def _stop(self, machine_ids=None):
        ids = list(machine_ids if machine_ids is not None else self.machine_ids)
        self.state = "working"
        texts = self.texts
        texts.set(self.explain_label, N_("Stopping printing..."))
        for button in (self.stop_button, self.back_button):
            button.pack_forget()
        self._make_rows(self.rows_frame, ids)
        reason, send_id = self.reason, self.send_id
        self.run_action(lambda actions, ids_, post, action_id: actions.stop(
            ids_, post, reason=reason, send_id=send_id, action_id=action_id), ids)

    def action_finished(self, result):
        failed = [m for m in result.machines if not m.ok]
        stopped = [m.machine_id for m in result.machines if m.ok]
        if failed:
            self._go_red(failed, stopped)
            return
        self.state = "done"
        texts = self.texts
        names = _joined(self.names(stopped or self.machine_ids))
        if self.reason == "rejected":
            lines = _lines_of(self.services, self.machine_ids)
            texts.set(self.explain_label, N_("Printing stopped on {machines}. {line} needs a new Send."),
                      machines=names, line=_joined(_line_names(self.services, lines)))
            window = self.services.nav.window(f"result:{self.send_id}") if self.send_id else None
            if window is not None and hasattr(window, "stopped"):
                for line_id in lines:
                    window.stopped(line_id)
        else:
            texts.set(self.explain_label, N_("Printing stopped on {machines}."), machines=names)
        _clear(self.buttons, texts)
        if self.reason == "rejected":
            self.fix_button = ttk.Button(self.buttons, style="Hx.Main.TButton", command=self._fix)
            texts.set(self.fix_button, N_("Fix and send again"))
            self.fix_button.pack(side="right")
        self.close_button = ttk.Button(self.buttons, command=self.destroy)
        texts.set(self.close_button, N_("Close"))
        self.close_button.pack(side="right", padx=(0, 8))

    def work_failed(self, msg):
        # The action never got going: nothing was stopped, so the operator must stop it at the printer
        failed = [MachineResult(machine_id=mid, colour=RED, ok=False, step=STEP_LOCK, msg=msg)
                  for mid in self._working_ids()]
        self._go_red(failed, [])

    def _working_ids(self):
        return list(self.rows) or list(self.machine_ids)

    def _go_red(self, failed, stopped):
        """Stop it at the printer now: full screen and red, with Try again (ui.md §8.1 item 7)."""
        self.state = "failed"
        self._failed_ids = [m.machine_id for m in failed]
        texts = self.texts
        self.page.pack_forget()
        if self.red_page is not None:
            _clear(self.red_page, texts)
            self.red_page.destroy()
        self.configure(background=RED_BG)
        page = self.red_page = tk.Frame(self, background=RED_BG, padx=32, pady=24)
        page.pack(fill="both", expand=True)
        heading = _red_label(page, 28, True)
        texts.set(heading, N_("Stop it at the printer now"))
        heading.pack(anchor="w")
        couldnt = _red_label(page, 16)
        texts.set(couldnt, N_("Stripes couldn't stop printing on {machines}."),
                  machines=_joined(self.names(self._failed_ids)))
        couldnt.pack(anchor="w", pady=(8, 0))
        press = _red_label(page, 14)
        texts.set(press, N_("Press Print On/Off on that printer now. Its card shows \"Stop it at the printer\" until "
                            "Stripes reads that printing is off."))
        press.pack(anchor="w", pady=(8, 8))
        for m, name in zip(failed, self.names(self._failed_ids)):
            problem = _red_label(page, 14)
            texts.set(problem, N_("{machine}: {problem}"), machine=name, problem=m.msg if m.msg is not None else "")
            problem.pack(anchor="w")
        if stopped:
            done = _red_label(page, 14)
            texts.set(done, N_("Printing stopped on {machines}."), machines=_joined(self.names(stopped)))
            done.pack(anchor="w", pady=(8, 0))
        buttons = tk.Frame(page, background=RED_BG)
        buttons.pack(fill="x", pady=(16, 0))
        self.close_button = ttk.Button(buttons, style="Hx.Big.TButton", command=self.destroy)
        texts.set(self.close_button, N_("Close"))
        self.close_button.pack(side="right")
        self.try_button = ttk.Button(buttons, style="Hx.Big.TButton", command=self._try_again)
        texts.set(self.try_button, N_("Try again"))
        self.try_button.pack(side="right", padx=(0, 12))
        _full_screen(self)
        self.show()

    def _try_again(self):
        """Stop the machines that failed again, with a new action id."""
        ids = list(self._failed_ids)
        if self.red_page is not None:
            _clear(self.red_page, self.texts)
            self.red_page.destroy()
            self.red_page = None
        _full_screen(self, False)
        self.configure(background=self._normal_bg)
        self.geometry("640x420")
        self.page.pack(fill="both", expand=True)
        self.result_label.show([])
        self.action_id = new_id("ac")
        self._stop(ids)

    def _fix(self):
        """Fix and send again: the Send window with these machines' lines filled in."""
        services = self.services
        lines = _lines_of(services, self.machine_ids)

        def open_with(request):
            identity = services.identity
            choices = [copy.deepcopy(c) for c in (request.lines if request is not None else []) if c.line_id in lines]
            if choices:
                services.nav.open_send(prefill=SendRequest(send_id="", sender=identity.user,
                                                           computer=identity.computer,
                                                           created_at=services.clock.time(), lines=choices))
            else:
                services.nav.open_send(line_ids=lines)
            self.destroy()

        if self.request is not None or not self.send_id:
            open_with(self.request)
            return

        def done(entry, error):
            if not _alive(self):
                return
            request = None
            if error is None and entry is not None:
                try:
                    request = hx_send.request_from_log(entry)
                except (KeyError, ValueError):
                    request = None
            open_with(request)

        services.bg.run(lambda: services.data.find_send(self.send_id), done)

    def close(self) -> None:
        if self.state == "working":
            hx_ui_common.tell(self, Msg(N_("Wait until Stripes has finished stopping printing.")))
            return
        self.destroy()


class ResumeDialog(_ActionDialog):
    """Resume printing, after loading what's on each printer and showing it (ui.md §8.2)."""
    kind = "resume"
    running_text = N_("Stripes is resuming printing on {machines}.")

    def __init__(self, master, services, *, machine_ids: list[str]):
        self.key = "resume:" + ",".join(sorted(machine_ids))
        super().__init__(master, services, title=N_("Resume printing"), machine_ids=machine_ids, size="900x640")
        self.state = "checking"
        self.checks = []
        self.pictures: dict[str, PictureView] = {}
        self.origins: dict[str, BagText] = {}
        self.close_button = None
        self._build()
        self.later(0, self._check)

    def _build(self):
        texts = self.texts
        body = ttk.Frame(self, padding=14)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(2, weight=1)
        heading = ttk.Label(body, style="Hx.Head.TLabel")
        texts.set(heading, N_("Resume printing"))
        heading.grid(row=0, column=0, sticky="w")
        self.status_label = ttk.Label(body, wraplength=820, justify="left")
        texts.set(self.status_label, N_("Loading what's on each printer..."))
        self.status_label.grid(row=1, column=0, sticky="w", pady=(4, 8))
        self.scroller = Scroller(body)
        self.scroller.grid(row=2, column=0, sticky="nsew")
        self.result_label = _Lines(body, style="Bad.TLabel", wrap=820)
        self.result_label.grid(row=3, column=0, sticky="w", pady=(8, 0))
        self.result_label.show([])
        self.buttons = ttk.Frame(body)
        self.buttons.grid(row=4, column=0, sticky="ew", pady=(10, 0))
        self.resume_button = ttk.Button(self.buttons, style="Hx.Main.TButton", command=self._resume)
        texts.set(self.resume_button, N_("Resume printing"))
        self.resume_button.pack(side="right")
        _enable(self.resume_button, False)
        self.back_button = ttk.Button(self.buttons, command=self.destroy)
        texts.set(self.back_button, N_("Go back"))
        self.back_button.pack(side="right", padx=(0, 8))

    def _check(self):
        services = self.services
        ids = list(self.machine_ids)

        def work():
            actions = services.actions()
            return [actions.check_resume(mid) for mid in ids]

        def done(checks, error):
            if not _alive(self):
                return
            if error is not None:
                self.state = "ready"
                self.texts.forget(self.status_label)
                self.status_label.configure(text="")
                self.result_label.show([_error_msg(error)])
                return
            self._show_checks(checks)

        services.bg.run(work, done)

    def _show_checks(self, checks):
        self.state = "ready"
        self.checks = list(checks)
        texts = self.texts
        inner = self.scroller.inner
        _clear(inner, texts)
        self.pictures.clear()
        self.origins.clear()
        names = dict(zip(self.machine_ids, self.names()))
        for check in self.checks:
            name = names.get(check.machine_id, check.machine_id)
            box = ttk.Frame(inner, padding=(0, 6))
            box.pack(fill="x", anchor="w", padx=4)
            ttk.Label(box, text=name, style="Hx.Section.TLabel").pack(anchor="w")
            picture = PictureView(box, width=640)
            picture.set_bmp(check.design.preview_bmp if check.design is not None else None)
            picture.pack(anchor="w", pady=(4, 4))
            self.pictures[check.machine_id] = picture
            origin_row = ttk.Frame(box)
            origin_row.pack(anchor="w")
            origin = BagText(origin_row, size=20, bold=True, wrap_px=800)
            origin.set(check.origin_line)
            origin.pack(side="left")
            self.origins[check.machine_id] = origin
            if not check.origin_line:
                unknown = ttk.Label(origin_row, style="Muted.TLabel")
                texts.set(unknown, N_("(origin unknown)"))
                unknown.pack(side="left", padx=(6, 0))
            verdict = ttk.Label(box, wraplength=800, justify="left")
            if check.allowed:
                verdict.configure(style="Ok.TLabel")
                texts.set(verdict, N_("This is the message of the last confirmed Send."))
            else:
                verdict.configure(style="Bad.TLabel")
                texts.set(verdict, N_("{machine}: {problem}"), machine=name,
                          problem=check.msg if check.msg is not None else "")
            verdict.pack(anchor="w")
        allowed = [c.machine_id for c in self.checks if c.allowed]
        if allowed and len(allowed) < len(self.checks):
            texts.set(self.status_label, N_("Only the machines that passed the check are resumed."))
        else:
            texts.forget(self.status_label)
            self.status_label.configure(text="")
        _enable(self.resume_button, bool(allowed) and self.state == "ready")

    def _resume(self):
        allowed = [c.machine_id for c in self.checks if c.allowed]
        if not allowed or self.state != "ready":
            return
        self.state = "working"
        _enable(self.resume_button, False)
        _enable(self.back_button, False)
        self.run_action(lambda actions, ids, post, action_id: actions.resume(ids, post, action_id=action_id),
                        allowed)

    def action_finished(self, result):
        self.state = "done"
        texts = self.texts
        resumed = [m.machine_id for m in result.machines if m.ok]
        failed = [m for m in result.machines if not m.ok]
        if resumed:
            texts.set(self.status_label, N_("Printing resumed on {machines}."), machines=_joined(self.names(resumed)))
        else:
            texts.forget(self.status_label)
            self.status_label.configure(text="")
        names = dict(zip(self.machine_ids, self.names()))
        self.result_label.show([Msg(N_("{machine}: {problem}"), {"machine": names.get(m.machine_id, m.machine_id),
                                                                  "problem": m.msg if m.msg is not None else ""})
                                for m in failed])
        self._closing_buttons()

    def work_failed(self, msg):
        self.state = "done"
        self.result_label.show([msg])
        self._closing_buttons()

    def _closing_buttons(self):
        _clear(self.buttons, self.texts)
        self.close_button = ttk.Button(self.buttons, command=self.destroy)
        self.texts.set(self.close_button, N_("Close"))
        self.close_button.pack(side="right")

    def close(self) -> None:
        if self.state == "working":
            hx_ui_common.tell(self, Msg(self.running_text, {"machines": _joined(self.names())}))
            return
        self.destroy()


class SetClocksDialog(_ActionDialog):
    """Set every printer's clock on some lines from the time server, without a Send (ui.md §8.3)."""
    kind = "clock"
    running_text = N_("Stripes is setting the clocks on {machines}.")

    def __init__(self, master, services, *, line_ids: list[str]):
        self.key = "clocks:" + ",".join(sorted(line_ids))
        self.line_ids = list(line_ids)
        data = services.data.current
        machine_ids = []
        if data is not None:
            for line_id in self.line_ids:
                for machine in data.line_machines(line_id):
                    if _printer_ready(data, machine):
                        machine_ids.append(machine.id)
        super().__init__(master, services, title=N_("Set clocks"), machine_ids=machine_ids)
        self.state = "ask"
        self.close_button = None
        self._notes: dict[str, ttk.Label] = {}
        self._build()

    def _build(self):
        texts = self.texts
        body = ttk.Frame(self, padding=14)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(2, weight=1)
        self.question_label = ttk.Label(body, style="Hx.Section.TLabel", wraplength=600, justify="left")
        texts.set(self.question_label, N_("Set the clock of every printer on {lines} from the time server?"),
                  lines=_joined(_line_names(self.services, self.line_ids)))
        self.question_label.grid(row=0, column=0, sticky="w")
        explain = ttk.Label(body, wraplength=600, justify="left")
        texts.set(explain, N_("Stripes sets the date, the time and the midnight date change. Nothing else on the "
                              "printers changes."))
        explain.grid(row=1, column=0, sticky="w", pady=(6, 8))
        self.rows_frame = ttk.Frame(body)
        self.rows_frame.grid(row=2, column=0, sticky="nsew")
        names = ttk.Label(self.rows_frame, text="   ".join(self.names()), wraplength=600, justify="left")
        names.pack(anchor="w")        # record names: data
        self.result_label = _Lines(body, style="Bad.TLabel", wrap=600)
        self.result_label.grid(row=3, column=0, sticky="w", pady=(8, 0))
        self.result_label.show([])
        self.buttons = ttk.Frame(body)
        self.buttons.grid(row=4, column=0, sticky="ew", pady=(10, 0))
        self.set_button = ttk.Button(self.buttons, style="Hx.Main.TButton", command=self._set)
        texts.set(self.set_button, N_("Set clocks"))
        self.set_button.pack(side="right")
        _enable(self.set_button, bool(self.machine_ids))
        self.back_button = ttk.Button(self.buttons, command=self.destroy)
        texts.set(self.back_button, N_("Go back"))
        self.back_button.pack(side="right", padx=(0, 8))

    def _set(self):
        if self.state != "ask" or not self.machine_ids:
            return
        self.state = "working"
        _enable(self.set_button, False)
        _enable(self.back_button, False)
        self._make_rows(self.rows_frame, self.machine_ids)
        self.run_action(lambda actions, ids, post, action_id: actions.set_clocks(ids, post, action_id=action_id),
                        self.machine_ids)

    def action_finished(self, result):
        self.state = "done"
        texts = self.texts
        for m in result.machines:
            row = self.rows.get(m.machine_id)
            if row is None:
                continue
            if m.ok:
                row.mark_label.configure(text="✓", style="Ok.TLabel")
                texts.set(row.msg_label, N_("Clock set"))
                row.msg_label.configure(style="Ok.TLabel")
            else:
                row.mark_label.configure(text="✗", style="Bad.TLabel")
                texts.msg(row.msg_label, m.msg)
                row.msg_label.configure(style="Bad.TLabel")
            if "pc_time" in m.notes:
                note = ttk.Label(row, style="Warn.TLabel", wraplength=420, justify="left")
                texts.set(note, N_("The time server didn't answer, so the PC's time was used."))
                note.pack(side="left", anchor="n", padx=(8, 0))
                self._notes[m.machine_id] = note
        self._closing_buttons()

    def work_failed(self, msg):
        self.state = "done"
        for row in self.rows.values():
            row.mark_label.configure(text="✗", style="Bad.TLabel")
            self.texts.msg(row.msg_label, msg)
            row.msg_label.configure(style="Bad.TLabel")
        if not self.rows:
            self.result_label.show([msg])
        self._closing_buttons()

    def _closing_buttons(self):
        _clear(self.buttons, self.texts)
        self.close_button = ttk.Button(self.buttons, command=self.destroy)
        self.texts.set(self.close_button, N_("Close"))
        self.close_button.pack(side="right")

    def close(self) -> None:
        if self.state == "working":
            hx_ui_common.tell(self, Msg(self.running_text, {"machines": _joined(self.names())}))
            return
        self.destroy()
