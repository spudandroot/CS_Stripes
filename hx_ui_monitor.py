"""The MapleJet line cards, the MapleJet monitor, the floor board and the alert sound.

LineBoard shows a BoardView as one card per line, with a row per machine; it sits in the
MapleJet tab and in the MapleJet monitor. FloorBoard is the monitor in floor-board mode:
one big row per machine, drawn on a canvas, for the production-schedule PC. AlertSounder
rings the alert sound while an unacknowledged sound alert is meant for this PC, and opens
the personal pop-up for alerts addressed to the person signed in. UserSettingsDialog holds
the three floor-board settings.

Nothing here decides a colour, a state or whether a button is allowed: those come from the
board's views (hx_state). Bag text (origins) is only ever shown in BagText canvases or as
canvas text, so the language switch can't touch it.
"""

from __future__ import annotations

import dataclasses
import time
import tkinter as tk
import tkinter.font
from dataclasses import dataclass
from datetime import datetime
from tkinter import ttk

import hx_state
import hx_ui_common
import hx_win
from hx_core import Msg
from hx_model import AMBER, BLUE, GREEN, GREY, RED, BoardUpdate, BoardView, DataUpdate, HxError, same_user
from hx_ui_common import (CARD_BG, COLOUR_HEX, DIM_FG, MUTED_FG, PAGE_BG, RULE_FG, TEXT_FG, UI_FONT, BagText,
                          ChoiceBox, HxSettings, HxWindow, Scroller, Texts, bag_font_family, check_tk_thread, roles,
                          text, when)
from i18n import N_, add_spanish, tr

ES = {
    "Stop line...": "Detener la línea...",
    "Resume line...": "Reanudar la línea...",
    "Stop...": "Detener...",
    "Resume...": "Reanudar...",
    "Confirm...": "Confirmar...",
    "Set up...": "Configurar...",
    "acknowledged by {user}": "reconocida por {user}",
    "not acknowledged": "sin reconocer",
    "{n} red": "{n} en rojo",
    "{n} amber": "{n} en ámbar",
    "{n} in progress": "{n} en curso",
    "{n} grey": "{n} en gris",
    "{n} green": "{n} en verde",
    "MapleJet alert": "Alerta MapleJet",
    "since {time}": "desde {time}",
    "The sound repeats until someone acknowledges.": "El sonido se repite hasta que alguien lo reconozca.",
    "Ask a Stripes MapleJet Sender or a Stripes Administrator to acknowledge it.":
        "Pida a un miembro de Stripes MapleJet Senders o de Stripes Administrators que la reconozca.",
    "Open MapleJet monitor": "Abrir el monitor MapleJet",
    "MapleJet floor board": "Tablero de planta MapleJet",
    "My MapleJet settings": "Mi configuración de MapleJet",
    "Open the floor board when Stripes starts": "Abrir el tablero de planta cuando Stripes inicie",
    "Screen": "Pantalla",
    "Full screen": "Pantalla completa",
    "Window": "Ventana",
    "The floor board opens whenever Stripes starts on this PC, including when you sign in to Windows.":
        "El tablero de planta se abre cada vez que Stripes inicia en esta PC, también al iniciar sesión en Windows.",
    "{n}: {width} x {height} (main)": "{n}: {width} x {height} (principal)",
    "{n}: {width} x {height}": "{n}: {width} x {height}",
    "This screen": "Esta pantalla",
    "Open the floor board now": "Abrir el tablero de planta ahora",
    # Also hx_ui's (ui.md §16.5): the monitor and floor board say it while the settings load
    "Loading the MapleJet settings...": "Cargando la configuración de MapleJet...",
}
add_spanish(ES)

FLOOR_MIN_ROWS = 4          # a floor-board row is never taller than 1/4 of the rows area
FLOOR_FLASH_MS = 1000

_TICK_MS = 1000             # ages and the clock
_REDRAW_MS = 100            # the floor board redraws this long after its last resize
_REFUSED_MS = 3000          # "You can refresh once every ... s." shows this long
_TWO_COLUMNS_PX = 1100      # line cards in two columns from this board width
_ORIGIN_WRAP_PX = 520
_SEPARATOR = "#e5e7eb"      # between rows of one line on the floor board
_SUMMARY = ((RED, N_("{n} red")), (AMBER, N_("{n} amber")), (BLUE, N_("{n} in progress")),
            (GREY, N_("{n} grey")), (GREEN, N_("{n} green")))
_SIZE_LIMITS = ((640, 7680), (360, 4320))   # the settings dialog's width and height Spinboxes


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------

@dataclass(frozen=True, kw_only=True)
class FloorLayout:
    header_h: int
    row_h: float
    bar_w: int
    names_w: int
    origin_w: int
    status_w: int
    age_w: int
    header_px: int
    line_px: int
    machine_px: int
    origin_px: int
    status_px: int
    age_px: int


def floor_layout(width: int, height: int, rows: int) -> FloorLayout:
    """Column widths, the row height and the font sizes (pixels) of the floor board (ui.md §10.3).
    Rows fill the screen, each at most a quarter of it; beyond 12 they keep shrinking."""
    header_h = max(48, round(height * 0.07))
    row_h = (height - header_h) / max(rows, FLOOR_MIN_ROWS)
    bar_w, names_w = round(width * 0.03), round(width * 0.17)
    origin_w, status_w = round(width * 0.45), round(width * 0.25)
    return FloorLayout(header_h=header_h, row_h=row_h, bar_w=bar_w, names_w=names_w, origin_w=origin_w,
                       status_w=status_w, age_w=width - bar_w - names_w - origin_w - status_w,
                       header_px=max(12, round(header_h * 0.45)), line_px=max(8, round(row_h * 0.20)),
                       machine_px=max(9, round(row_h * 0.28)), origin_px=max(10, round(row_h * 0.50)),
                       status_px=max(9, round(row_h * 0.30)), age_px=max(8, round(row_h * 0.20)))


def board_summary(board: BoardView) -> list[Msg]:
    """Machines counted by colour, worst first, non-zero counts only: "1 red", "4 green"."""
    counts = {}
    for line in board.lines:
        for m in line.machines:
            counts[m.colour] = counts.get(m.colour, 0) + 1
    return [Msg(label, {"n": counts[colour]}) for colour, label in _SUMMARY if counts.get(colour)]


def banner_now(board: BoardView | None, elapsed_s: float = 0.0) -> Msg | None:
    """board_banner(board) with its "next check in N s" counted down by the seconds since the
    BoardUpdate arrived (never below 0)."""
    if board is None:
        return None
    if board.standalone and board.next_check_s is not None:
        board = dataclasses.replace(board, next_check_s=max(0.0, board.next_check_s - max(0.0, elapsed_s)))
    return hx_state.board_banner(board)


def no_lines_texts(services) -> tuple[list[Msg], bool]:
    """What a board with no lines stands for, and whether it is a warning. While the settings
    aren't loaded the watcher posts an empty board (states.md §7 step 1): that is their problems,
    or that they are still loading (as the tab says, ui.md §4.6), not "No lines are set up yet.".
    Settings with active lines that the board doesn't show yet: the next board is on its way."""
    data = getattr(services.data, "current", None)
    if data is None:
        problems = list(getattr(services.data, "problems", None) or [])
        if problems:
            return problems, True
        return [Msg(N_("Loading the MapleJet settings..."))], False
    if any(line.active for line in data.lines.values()):
        return [Msg(N_("Checking..."))], False
    return [Msg(N_("No lines are set up yet."))], False


def _joined(msgs: list[Msg]) -> Msg:
    """Several texts as one, one per line, each still translated on its own."""
    if len(msgs) == 1:
        return msgs[0]
    return Msg("\n".join("{m%d}" % i for i in range(len(msgs))), {f"m{i}": m for i, m in enumerate(msgs)})


def _age(age_s, elapsed_s):
    """A view's age moved on by the seconds since its board arrived; None stays None."""
    return None if age_s is None else age_s + max(0.0, elapsed_s)


def _alert_line(alert) -> str:
    """"♪ Send failed on ... · not acknowledged"."""
    acked = (tr("acknowledged by {user}", user=alert.acked_by) if alert.acked_by else tr("not acknowledged"))
    return ("♪ " if alert.sound else "") + text(alert.msg) + " · " + acked


def _unacked_ids(views) -> list[str]:
    """The active, unacknowledged alerts of these machines, each once."""
    ids = []
    for v in views:
        for a in v.alerts:
            if a.active and not a.acked_by and a.id not in ids:
                ids.append(a.id)
    return ids


def _show_widgets(widgets, wanted, **pack):
    """Pack exactly the wanted widgets, in the given order (role- and state-gated buttons)."""
    for w in widgets:
        w.pack_forget()
    for w in widgets:
        if w in wanted:
            w.pack(**pack)


def _enable(button, on):
    button.state(["!disabled"] if on else ["disabled"])


def _alive(widget):
    try:
        return bool(widget.winfo_exists())
    except tk.TclError:
        return False


# --------------------------------------------------------------------------
# Line cards
# --------------------------------------------------------------------------

class MachineRow(tk.Frame):
    """One machine inside a LineCard: its colour, headline, origin (bag text), details, alerts
    and the buttons its view allows (none for a BLUE machine)."""

    def __init__(self, master, board: "LineBoard", view):
        super().__init__(master, background=CARD_BG)
        self.board = board
        self.view = view
        texts = board.texts
        head = tk.Frame(self, background=CARD_BG)
        head.pack(fill="x")
        self.dot = tk.Canvas(head, width=14, height=14, background=CARD_BG, highlightthickness=0, borderwidth=0)
        self.dot_item = self.dot.create_oval(2, 2, 12, 12, outline="", fill=COLOUR_HEX.get(view.colour, RULE_FG))
        self.dot.pack(side="left", padx=(0, 6))
        self.name_label = tk.Label(head, font=(UI_FONT, 11, "bold"), background=CARD_BG, foreground=TEXT_FG)
        self.name_label.pack(side="left")
        self.age_label = tk.Label(head, font=(UI_FONT, 8), background=CARD_BG, foreground=MUTED_FG)
        self.age_label.pack(side="right")
        self.headline_label = tk.Label(head, background=CARD_BG, anchor="w", justify="left")
        self.headline_label.pack(side="left", padx=(10, 0), fill="x", expand=True)

        body = tk.Frame(self, background=CARD_BG)
        body.pack(fill="x", padx=(20, 0))
        self.origin = BagText(body, size=13, bold=True, wrap_px=_ORIGIN_WRAP_PX)
        self.origin.pack(anchor="w")
        self.unknown_label = tk.Label(body, background=CARD_BG, foreground=MUTED_FG, anchor="w")
        texts.set(self.unknown_label, N_("(origin unknown)"))
        self.details_label = tk.Label(body, font=(UI_FONT, 8), background=CARD_BG, foreground=MUTED_FG,
                                      anchor="w", justify="left", wraplength=_ORIGIN_WRAP_PX)
        self.details_label.pack(anchor="w", fill="x")
        self.alerts_frame = tk.Frame(body, background=CARD_BG)
        self.alerts_frame.pack(anchor="w", fill="x")
        self.alert_labels: list[tk.Label] = []
        self.buttons = tk.Frame(body, background=CARD_BG)
        self.stop_button = ttk.Button(self.buttons, command=lambda: self._nav().stop([self.view.machine_id]))
        self.resume_button = ttk.Button(self.buttons, command=lambda: self._nav().resume([self.view.machine_id]))
        self.send_button = ttk.Button(self.buttons, style="Hx.Main.TButton",
                                      command=lambda: self._nav().open_send(line_ids=[self.view.line_id]))
        self.confirm_button = ttk.Button(self.buttons, style="Hx.Main.TButton",
                                         command=lambda: self._nav().open_result(self.view.confirm_send_id))
        self.ack_button = ttk.Button(self.buttons, command=self._acknowledge)
        self.setup_button = ttk.Button(self.buttons, command=lambda: self._nav().open_admin(
            page="machines", record_id=self.view.machine_id))
        for button, label in ((self.stop_button, N_("Stop...")), (self.resume_button, N_("Resume...")),
                              (self.send_button, N_("Send...")), (self.confirm_button, N_("Confirm...")),
                              (self.ack_button, N_("Acknowledge")), (self.setup_button, N_("Set up..."))):
            texts.set(button, label)
        self._buttons = [self.stop_button, self.resume_button, self.send_button, self.confirm_button,
                         self.ack_button, self.setup_button]
        self.update_view(view, hx_ui_common.Roles(), dim=False)

    def _nav(self):
        return self.board.services.nav

    def _acknowledge(self):
        ids = _unacked_ids([self.view])
        if ids:
            self.board.services.watcher.acknowledge(ids)

    def update_view(self, view, roles_now, *, dim: bool) -> None:
        """Show the machine's view in place (no flicker)."""
        self.view = view
        colour = COLOUR_HEX.get(view.colour, COLOUR_HEX[GREY])
        self.dot.itemconfigure(self.dot_item, fill=DIM_FG if dim else colour)
        self.name_label.configure(text=view.name, foreground=DIM_FG if dim else TEXT_FG)
        self.headline_label.configure(text=text(view.headline), foreground=DIM_FG if dim else colour)
        self.origin.set(view.origin_line)
        self.origin.set_colours(foreground=DIM_FG if dim else TEXT_FG)
        if view.origin_line:
            self.unknown_label.pack_forget()
        else:
            self.unknown_label.pack(anchor="w", after=self.origin)
        alert_texts = [text(a.msg) for a in view.alerts]
        details = [text(d) for d in view.details]
        details = [d for d in details if d and d not in alert_texts]
        self.details_label.configure(text="\n".join(details), foreground=DIM_FG if dim else MUTED_FG)
        if details:
            self.details_label.pack(anchor="w", fill="x", before=self.alerts_frame)  # alerts_frame stays packed
        else:
            self.details_label.pack_forget()
        for label in self.alert_labels:
            label.destroy()
        self.alert_labels = []
        for a in view.alerts:
            label = tk.Label(self.alerts_frame, text=_alert_line(a), background=CARD_BG, anchor="w", justify="left",
                             wraplength=_ORIGIN_WRAP_PX,
                             foreground=DIM_FG if dim else COLOUR_HEX.get(a.colour or AMBER, COLOUR_HEX[AMBER]))
            label.pack(anchor="w", fill="x")
            self.alert_labels.append(label)
        self.update_age(0.0)
        # Buttons: only what the view and the person's roles allow, so rows stay short
        wanted = set()
        if view.colour != BLUE:
            if roles_now.sender and view.can_stop:
                wanted.add(self.stop_button)
            if roles_now.sender and view.can_resume:
                wanted.add(self.resume_button)
            if roles_now.sender and view.needs_send:
                wanted.add(self.send_button)
            if view.confirm_pending_by and same_user(view.confirm_pending_by, self.board.services.identity.user):
                wanted.add(self.confirm_button)
            if roles_now.acknowledge and _unacked_ids([view]):
                wanted.add(self.ack_button)
            if roles_now.admin and view.code == "not_set_up":
                wanted.add(self.setup_button)
        _show_widgets(self._buttons, wanted, side="left", padx=(0, 6), pady=(4, 0))
        if wanted:
            self.buttons.pack(anchor="w", fill="x")
        else:
            self.buttons.pack_forget()

    def update_age(self, elapsed_s: float) -> None:
        self.age_label.configure(text=text(hx_state.checked_text(_age(self.view.checked_age_s, elapsed_s))))


class LineCard(tk.Frame):
    """One line: a colour bar, the name, headline and age, the line buttons and a row per machine."""

    def __init__(self, master, board: "LineBoard", line):
        super().__init__(master, background=RULE_FG, padx=1, pady=1)
        self.board = board
        self.line = line
        texts = board.texts
        self.bar = tk.Frame(self, width=6, background=COLOUR_HEX.get(line.colour, RULE_FG))
        self.bar.pack(side="left", fill="y")
        self.body = tk.Frame(self, background=CARD_BG, padx=10, pady=8)
        self.body.pack(side="left", fill="both", expand=True)
        self.head = head = tk.Frame(self.body, background=CARD_BG)
        head.pack(fill="x")
        self.name_label = tk.Label(head, font=(UI_FONT, 13, "bold"), background=CARD_BG, foreground=TEXT_FG)
        self.name_label.pack(side="left")
        self.age_label = tk.Label(head, font=(UI_FONT, 8), background=CARD_BG, foreground=MUTED_FG)
        self.age_label.pack(side="right")
        self.headline_label = tk.Label(head, background=CARD_BG, anchor="e")
        self.headline_label.pack(side="right", padx=(10, 8))
        self.buttons = tk.Frame(self.body, background=CARD_BG)
        self.buttons.pack(fill="x", pady=(6, 0))
        self.send_button = ttk.Button(self.buttons, command=lambda: board.services.nav.open_send(
            line_ids=[self.line.line_id]))
        self.stop_button = ttk.Button(self.buttons, command=lambda: board.services.nav.stop(
            [m.machine_id for m in self.line.machines if m.can_stop]))
        self.resume_button = ttk.Button(self.buttons, command=lambda: board.services.nav.resume(
            [m.machine_id for m in self.line.machines if m.can_resume]))
        self.clocks_button = ttk.Button(self.buttons, command=lambda: board.services.nav.set_clocks(
            [self.line.line_id]))
        self.ack_button = ttk.Button(self.buttons, command=self._acknowledge)
        for button, label in ((self.send_button, N_("Send...")), (self.stop_button, N_("Stop line...")),
                              (self.resume_button, N_("Resume line...")), (self.clocks_button, N_("Set clocks...")),
                              (self.ack_button, N_("Acknowledge"))):
            texts.set(button, label)
        self._buttons = [self.send_button, self.stop_button, self.resume_button, self.clocks_button, self.ack_button]
        tk.Frame(self.body, height=1, background=RULE_FG).pack(fill="x", pady=6)
        self.rows: dict[str, MachineRow] = {}
        for view in line.machines:
            row = MachineRow(self.body, board, view)
            row.pack(fill="x", pady=(0, 8))
            self.rows[view.machine_id] = row
        self.update_line(line, hx_ui_common.Roles())

    def _acknowledge(self):
        ids = _unacked_ids(self.line.machines)
        if ids:
            self.board.services.watcher.acknowledge(ids)

    def update_line(self, line, roles_now) -> None:
        self.line = line
        # Greyed out only when nobody is checking any of its machines (its headline then reads
        # NOT MONITORED); a line with one machine watched keeps its colours
        dim = not any(m.monitored for m in line.machines)
        colour = COLOUR_HEX.get(line.colour, COLOUR_HEX[GREY])
        self.bar.configure(background=colour)
        self.name_label.configure(text=line.name, foreground=DIM_FG if dim else TEXT_FG)
        self.headline_label.configure(text=text(line.headline), foreground=DIM_FG if dim else colour)
        wanted = set()
        if roles_now.sender:
            wanted |= {self.send_button, self.stop_button, self.resume_button, self.clocks_button}
        if roles_now.acknowledge:
            wanted.add(self.ack_button)
        _show_widgets(self._buttons, wanted, side="left", padx=(0, 6))
        if wanted:
            self.buttons.pack(fill="x", pady=(6, 0), after=self.head)
        else:
            self.buttons.pack_forget()
        machines = line.machines
        self.send_button.configure(style="Hx.Main.TButton" if any(m.needs_send for m in machines) else "TButton")
        _enable(self.stop_button, any(m.can_stop for m in machines))
        _enable(self.resume_button, any(m.can_resume for m in machines))
        _enable(self.clocks_button, any(m.colour != BLUE for m in machines))
        _enable(self.ack_button, bool(_unacked_ids(machines)))
        for view in machines:
            row = self.rows.get(view.machine_id)
            if row is not None:
                row.update_view(view, roles_now, dim=dim)
        self.update_age(0.0)

    def update_age(self, elapsed_s: float) -> None:
        self.age_label.configure(text=text(hx_state.checked_text(_age(self.line.checked_age_s, elapsed_s))))
        for row in self.rows.values():
            row.update_age(elapsed_s)


class LineBoard(ttk.Frame):
    """Line and machine cards, embedded in the MapleJet tab and in MonitorWindow. It subscribes to
    BoardUpdate itself (and unsubscribes in destroy()) and shows watcher.latest when built."""

    def __init__(self, master, services):
        super().__init__(master)
        self.services = services
        self.texts = Texts()
        self.cards: dict[str, LineCard] = {}
        self.board = None
        self.received = None            # services.clock.monotonic() when the board arrived
        self.roles = hx_ui_common.Roles()
        self._shape = None              # [(line id, [machine ids])] of the cards on show
        self._columns = 0
        self._tokens = []
        self._timer = None
        self._ticking = True
        self._destroyed = False
        self.scroller = Scroller(self, background=PAGE_BG)
        self.scroller.pack(fill="both", expand=True)
        self.message_label = tk.Label(self.scroller.inner, background=PAGE_BG, foreground=MUTED_FG,
                                      font=(UI_FONT, 11), justify="left")
        self._subscribe(BoardUpdate, lambda e: self.show(e.board))
        self._subscribe(DataUpdate, lambda e: self._roles_changed())
        self.bind("<Configure>", self._on_configure, add="+")
        latest = getattr(services.watcher, "latest", None)
        if latest is not None:
            self.show(latest)
        else:
            self._message([Msg(N_("Checking..."))])
        self._timer = self.after(_TICK_MS, self._tick)

    # events and timers

    def _subscribe(self, event_type, callback):
        def deliver(event):
            check_tk_thread()
            if not self._destroyed:
                callback(event)
        self._tokens.append(self.services.bus.subscribe(event_type, deliver))

    def start_tick(self) -> None:
        self._ticking = True

    def stop_tick(self) -> None:
        self._ticking = False

    def _tick(self):
        self._timer = None
        if self._destroyed:
            return
        try:
            if self._ticking and self.winfo_viewable():
                self.update_ages()
        except tk.TclError:
            return
        self._timer = self.after(_TICK_MS, self._tick)

    def destroy(self) -> None:
        if not self._destroyed:
            self._destroyed = True
            for token in self._tokens:
                self.services.bus.unsubscribe(token)
            self._tokens.clear()
            if self._timer is not None:
                try:
                    self.after_cancel(self._timer)
                except tk.TclError:
                    pass
                self._timer = None
        super().destroy()

    # showing

    def elapsed(self) -> float:
        """Seconds since the board on show arrived."""
        if self.received is None:
            return 0.0
        return max(0.0, self.services.clock.monotonic() - self.received)

    def _message(self, msgs: list[Msg], *, warn: bool = False):
        """Text in place of the cards: "Checking...", the settings' problems or "No lines are set up yet."."""
        for card in self.cards.values():
            card.destroy()
        self.cards = {}
        self._shape = None
        self.message_label.configure(foreground=COLOUR_HEX[AMBER] if warn else MUTED_FG)
        self.texts.msg(self.message_label, _joined(msgs))
        self.message_label.grid(row=0, column=0, padx=16, pady=24, sticky="w")

    def show(self, board) -> None:
        """Show a BoardView: cards rebuilt when the lines or machines changed, else updated in place."""
        check_tk_thread()
        self.board = board
        self.received = self.services.clock.monotonic()
        self.roles = roles(self.services)
        if board is None:
            self._message([Msg(N_("Checking..."))])
            return
        if not board.lines:
            msgs, warn = no_lines_texts(self.services)
            self._message(msgs, warn=warn)
            return
        shape = [(line.line_id, [m.machine_id for m in line.machines]) for line in board.lines]
        if shape != self._shape:
            self.message_label.grid_forget()
            self.texts.forget(self.message_label)
            for card in self.cards.values():
                card.destroy()
            self.cards = {}
            for line in board.lines:
                self.cards[line.line_id] = LineCard(self.scroller.inner, self, line)
            self._shape = shape
            self._columns = 0
            self._arrange()
        for line in board.lines:
            self.cards[line.line_id].update_line(line, self.roles)

    def _roles_changed(self):
        if self.board is not None:
            self.show_again()

    def show_again(self) -> None:
        """Render the board on show again (new roles, a new language), keeping its age."""
        received = self.received
        self.show(self.board)
        if received is not None:
            self.received = received
            self.update_ages()

    def update_ages(self) -> None:
        """Every "checked N s ago" moved on by the seconds since the board arrived."""
        elapsed = self.elapsed()
        for card in self.cards.values():
            card.update_age(elapsed)

    def _wanted_columns(self):
        width = self.winfo_width()
        return 2 if width >= _TWO_COLUMNS_PX else 1

    def _on_configure(self, event):
        if event.widget is self and self.cards and self._wanted_columns() != self._columns:
            self._arrange()

    def _arrange(self):
        columns = self._wanted_columns()
        self._columns = columns
        inner = self.scroller.inner
        for c in range(2):
            inner.columnconfigure(c, weight=1 if c < columns else 0, uniform="cards" if c < columns else "")
        for i, card in enumerate(self.cards.values()):
            card.grid(row=i // columns, column=i % columns, sticky="new", padx=6, pady=6)

    def retranslate(self) -> None:
        """The cards' text in the current language; origins (bag text) aren't touched."""
        self.texts.apply()
        if self.board is not None:
            self.show_again()


# --------------------------------------------------------------------------
# The MapleJet monitor
# --------------------------------------------------------------------------

class MonitorWindow(HxWindow):
    """"MapleJet monitor": the line cards in their own window. While it is open this PC keeps
    polling in standalone mode (Navigator.update_monitor_flag)."""
    key = "monitor"

    def __init__(self, master, services):
        super().__init__(master, services, title=N_("MapleJet monitor"))
        self.geometry("1180x780")
        self.minsize(760, 480)
        self.configure(background=PAGE_BG)
        self.board_view = None
        self.updated_at = ""            # when the last board arrived, as shown
        self._refused_timer = None
        bar = ttk.Frame(self, padding=(14, 12, 14, 6))
        bar.pack(fill="x")
        self.refresh_button = ttk.Button(bar, command=self._refresh)
        self.texts.set(self.refresh_button, N_("Refresh now"))
        self.refresh_button.pack(side="right")
        self.floor_button = ttk.Button(bar, command=lambda: services.nav.open_floor_board())
        self.texts.set(self.floor_button, N_("Floor board"))
        self.floor_button.pack(side="right", padx=(0, 8))
        titles = ttk.Frame(bar)
        titles.pack(side="left", fill="x", expand=True)
        self.heading_label = ttk.Label(titles, style="Hx.Head.TLabel")
        self.texts.set(self.heading_label, N_("MapleJet monitor"))
        self.heading_label.pack(anchor="w")
        self.summary_label = ttk.Label(titles, style="Muted.TLabel")
        self.summary_label.pack(anchor="w")
        self.refused_label = ttk.Label(titles, style="Warn.TLabel")
        self.banner_label = ttk.Label(titles, style="Warn.TLabel")
        self.board = LineBoard(self, services)
        self.board.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.subscribe(BoardUpdate, lambda e: self._show(e.board))
        latest = getattr(services.watcher, "latest", None)
        if latest is not None:
            self._show(latest)
        else:
            self.summary_label.configure(text=tr("Checking..."))
        self.later(_TICK_MS, self._tick)

    def _show(self, board):
        self.board_view = board
        self.updated_at = datetime.now().strftime("%I:%M:%S %p").lstrip("0")
        self._show_summary()

    def _show_summary(self):
        board = self.board_view
        if board is None:
            self.summary_label.configure(text=tr("Checking..."))
        else:
            parts = [text(m) for m in board_summary(board)]
            parts.append(tr("updated {time}", time=self.updated_at))
            self.summary_label.configure(text=" · ".join(parts))
        self._show_banner()

    def refresh_texts(self) -> None:
        self._show_summary()
        self.board.retranslate()

    def _show_banner(self):
        banner = banner_now(self.board_view, self.board.elapsed())
        if banner is None:
            self.banner_label.pack_forget()
        else:
            self.banner_label.configure(text=text(banner))
            self.banner_label.pack(anchor="w")

    def _tick(self):
        self._show_banner()
        self.later(_TICK_MS, self._tick)

    def _refresh(self):
        services = self.services
        if services.poller.mode == "standalone":
            if not services.poller.refresh_now():
                self.texts.set(self.refused_label, N_("You can refresh once every {seconds} s."),
                               seconds=int(services.timings.refresh_now_min_s))
                self.refused_label.pack(anchor="w", after=self.summary_label)
                if self._refused_timer is not None:
                    self.after_cancel(self._refused_timer)
                self._refused_timer = self.later(_REFUSED_MS, self._hide_refused)
        else:
            services.watcher.refresh_now()

    def _hide_refused(self):
        self._refused_timer = None
        self.refused_label.pack_forget()

    def destroy(self) -> None:
        nav = getattr(self.services, "nav", None)
        super().destroy()
        _flag_changed(nav)


def _flag_changed(nav):
    """A monitor or the floor board opened or closed: tell the Navigator."""
    update = getattr(nav, "update_monitor_flag", None)
    if update is None:
        return
    try:
        update()
    except tk.TclError:     # the app is closing
        pass


# --------------------------------------------------------------------------
# The floor board
# --------------------------------------------------------------------------

class FloorBoard(HxWindow):
    """The MapleJet monitor in floor-board mode: one big row per machine on one canvas
    (ui.md §10). Full screen is a borderless window on the chosen screen, not topmost; Escape
    does nothing, and the board stays until Close."""
    key = "floor"

    def __init__(self, master, services, *, screen: int = 0, size: str = "full"):
        super().__init__(master, services, title=N_("MapleJet floor board"))
        self.screen = screen
        self.size = size
        self.board = None
        self.received = None
        self.rows = []
        self.views = {}
        self.layout = None
        self.origin_items: dict[str, int] = {}
        self.unknown_items: dict[str, int] = {}
        self.status_items: dict[str, int] = {}
        self.age_items: dict[str, int] = {}
        self.bar_items: dict[str, int] = {}
        self.flash_on = True
        self.title_item = self.clock_item = self.banner_item = None
        self.message_item = None        # the text that stands in for the rows when there are none
        self._fonts = {}
        self._redraw_timer = None
        self._refused_until = None
        self.canvas = tk.Canvas(self, background=CARD_BG, highlightthickness=0, borderwidth=0)
        self.canvas.pack(fill="both", expand=True)
        self.refresh_button = ttk.Button(self.canvas, command=self._refresh)
        self.ack_button = ttk.Button(self.canvas, command=self._acknowledge)
        self.close_button = ttk.Button(self.canvas, command=self.close)
        self.texts.set(self.refresh_button, N_("Refresh now"))
        self.texts.set(self.ack_button, N_("Acknowledge"))
        self.texts.set(self.close_button, N_("Close"))
        self.monitor = self._screen()
        self._place()
        self.subscribe(BoardUpdate, lambda e: self.show(e.board))
        self.subscribe(DataUpdate, lambda e: self.redraw())
        self.canvas.bind("<Configure>", self._on_configure, add="+")
        self.later(_TICK_MS, self._tick_timer)
        self.later(FLOOR_FLASH_MS, self._flash_timer)
        self.show(getattr(services.watcher, "latest", None))

    # where and how big

    def _screen(self):
        mons = hx_win.monitors()
        if mons:
            return mons[self.screen] if 0 <= self.screen < len(mons) else mons[0]
        return hx_win.Monitor(0, 0, 0, self.winfo_screenwidth(), self.winfo_screenheight(), True)

    def _place(self):
        m = self.monitor
        wanted = HxSettings(floor_board_size=self.size).size_tuple() if self.size != "full" else None
        if wanted is None:                  # "full", or anything that isn't a window size
            self.full_screen = True
            self.overrideredirect(True)
            self.geometry_spec = f"{m.width}x{m.height}+{m.x}+{m.y}"
        else:
            self.full_screen = False
            self.geometry_spec = f"{min(wanted[0], m.width)}x{min(wanted[1], m.height)}+{m.x}+{m.y}"
        self.geometry(self.geometry_spec)

    def _canvas_size(self):
        width, height = self.canvas.winfo_width(), self.canvas.winfo_height()
        if width <= 1 or height <= 1:       # not laid out yet: the size it was given
            size = self.geometry_spec.split("+", 1)[0]
            width, height = (int(n) for n in size.split("x"))
        return width, height

    # events

    def show(self, board) -> None:
        check_tk_thread()
        self.board = board
        self.received = self.services.clock.monotonic()
        self.rows = hx_state.floor_board_rows(board) if board is not None else []
        self.views = ({m.machine_id: m for line in board.lines for m in line.machines} if board is not None else {})
        self.redraw()

    def _on_configure(self, event):
        if self._redraw_timer is not None:
            self.after_cancel(self._redraw_timer)
        self._redraw_timer = self.later(_REDRAW_MS, self._debounced)

    def _debounced(self):
        self._redraw_timer = None
        self.redraw()

    def refresh_texts(self) -> None:
        self.redraw()

    def elapsed(self) -> float:
        if self.received is None:
            return 0.0
        return max(0.0, self.services.clock.monotonic() - self.received)

    def _tick_timer(self):
        self.tick()
        self.later(_TICK_MS, self._tick_timer)

    def _flash_timer(self):
        self.flash()
        self.later(FLOOR_FLASH_MS, self._flash_timer)

    def tick(self) -> None:
        """Ages, the clock and the banner's countdown, by itemconfigure only."""
        elapsed = self.elapsed()
        if self.clock_item is not None:
            self.canvas.itemconfigure(self.clock_item, text=_clock_text())
        if self.banner_item is not None:
            self.canvas.itemconfigure(self.banner_item, text=self._banner_text(elapsed))
        for r in self.rows:
            item = self.age_items.get(r.machine_id)
            if item is not None:
                self.canvas.itemconfigure(item, text=text(hx_state.checked_text(_age(r.checked_age_s, elapsed))))

    def flash(self) -> None:
        """The colour bar of a machine with an unacknowledged sound alert alternates with the card colour."""
        self.flash_on = not self.flash_on
        for r in self.rows:
            view = self.views.get(r.machine_id)
            item = self.bar_items.get(r.machine_id)
            if item is None or view is None or not view.unacked_sound:
                continue
            colour = COLOUR_HEX.get(r.colour, COLOUR_HEX[GREY])
            self.canvas.itemconfigure(item, fill=colour if self.flash_on else CARD_BG)

    def _banner_text(self, elapsed):
        if self._refused_until is not None and time.monotonic() < self._refused_until:
            return tr("You can refresh once every {seconds} s.",
                      seconds=int(self.services.timings.refresh_now_min_s))
        return text(banner_now(self.board, elapsed))

    def _refresh(self):
        if not self.services.poller.refresh_now():
            self._refused_until = time.monotonic() + _REFUSED_MS / 1000
            self.later(_REFUSED_MS + 50, self.tick)
        self.tick()

    def _sounding(self):
        if self.board is None:
            return []
        return hx_state.sounding_alerts(self.board, self.services.identity, floor_board=True)

    def _acknowledge(self):
        ids = [a.id for a in self._sounding()]
        if ids:
            self.services.watcher.acknowledge(ids)

    # drawing

    def _font(self, family, px, weight="normal"):
        key = (family, px, weight)
        if key not in self._fonts:
            self._fonts[key] = tkinter.font.Font(root=self, family=family, size=-px, weight=weight)
        return self._fonts[key]

    def redraw(self) -> None:
        """Draw the whole board again for the canvas's size."""
        check_tk_thread()
        c = self.canvas
        c.delete("all")
        self.message_item = None
        self.origin_items, self.unknown_items, self.status_items = {}, {}, {}
        self.age_items, self.bar_items = {}, {}
        width, height = self._canvas_size()
        self.layout = lay = floor_layout(width, height, len(self.rows))
        self._draw_header(width, lay)
        if not self.rows:
            # No board yet, the settings not loaded (their problems), or no machines to show
            warn = False
            if self.board is None:
                msgs = [Msg(N_("Checking..."))]
            elif not self.board.lines:
                msgs, warn = no_lines_texts(self.services)
            else:
                msgs = [Msg(N_("No lines are set up yet."))]
            self.message_item = c.create_text(
                width / 2, lay.header_h + (height - lay.header_h) / 2, text=text(_joined(msgs)),
                font=(UI_FONT, -lay.status_px, "bold"), fill=COLOUR_HEX[AMBER] if warn else MUTED_FG,
                justify="center", width=max(1, width - 32))
            return
        family = bag_font_family(self)
        elapsed = self.elapsed()
        for i, r in enumerate(self.rows):
            self._draw_row(i, r, lay, width, family, elapsed)
        self._sync_flash()

    def _draw_header(self, width, lay):
        c = self.canvas
        mid = lay.header_h / 2
        self.title_item = c.create_text(16, mid, anchor="w", text=tr("MapleJet floor board"),
                                        font=(UI_FONT, -lay.header_px, "bold"), fill=TEXT_FG)
        x = (c.bbox(self.title_item) or (0, 0, 16, 0))[2] + 24
        self.clock_item = c.create_text(x, mid, anchor="w", text=_clock_text(),
                                        font=(UI_FONT, -lay.header_px), fill=MUTED_FG)
        x = (c.bbox(self.clock_item) or (0, 0, x, 0))[2] + 24
        self.banner_item = c.create_text(x, mid, anchor="w", text=self._banner_text(self.elapsed()),
                                         font=(UI_FONT, -max(8, round(lay.header_px * 0.6))),
                                         fill=COLOUR_HEX[AMBER])
        c.create_line(0, lay.header_h, width, lay.header_h, fill=RULE_FG, width=1)
        # Buttons at the right, as canvas windows: Refresh now only in standalone mode,
        # Acknowledge only for someone who may and while something rings here
        shown = [self.close_button]
        if roles(self.services).acknowledge and self._sounding():
            shown.append(self.ack_button)
        if self.board is not None and self.board.standalone:
            shown.append(self.refresh_button)
        x = width - 16
        for button in shown:
            c.create_window(x, mid, anchor="e", window=button)
            x -= button.winfo_reqwidth() + 8

    def _draw_row(self, i, r, lay, width, family, elapsed):
        c = self.canvas
        y0 = round(lay.header_h + i * lay.row_h)
        y1 = round(lay.header_h + (i + 1) * lay.row_h)
        cy = (y0 + y1) / 2
        view = self.views.get(r.machine_id)
        dim = view is not None and view.code == "not_monitored"
        colour = COLOUR_HEX.get(r.colour, COLOUR_HEX[GREY])
        # 1. Colour bar (it keeps its colour on a NOT MONITORED row)
        self.bar_items[r.machine_id] = c.create_rectangle(0, y0, lay.bar_w, y1, fill=colour, outline="")
        # 2. Names
        x = lay.bar_w + 8
        line_h, machine_h = lay.line_px * 1.25, lay.machine_px * 1.25
        top = cy - (line_h + machine_h) / 2
        c.create_text(x, top, anchor="nw", text=r.line_name, width=max(1, lay.names_w - 16),
                      font=(UI_FONT, -lay.line_px), fill=DIM_FG if dim else MUTED_FG)
        c.create_text(x, top + line_h, anchor="nw", text=r.machine_name, width=max(1, lay.names_w - 16),
                      font=(UI_FONT, -lay.machine_px, "bold"), fill=DIM_FG if dim else TEXT_FG)
        # 3. Origin: bag text, as large as fits; never cut short
        x = lay.bar_w + lay.names_w + 8
        room = max(1, lay.origin_w - 16)
        if r.origin_line:
            px, wrap = self._origin_px(r.origin_line, family, lay.origin_px, room)
            self.origin_items[r.machine_id] = c.create_text(
                x, cy, anchor="w", text=r.origin_line, width=room if wrap else 0,
                font=(family, -px, "bold"), fill=DIM_FG if dim else TEXT_FG)
        else:
            self.origin_items[r.machine_id] = c.create_text(x, cy, anchor="w", text="", font=(family, -lay.origin_px))
            self.unknown_items[r.machine_id] = c.create_text(
                x, cy, anchor="w", text=tr("(origin unknown)"), font=(UI_FONT, -lay.status_px),
                fill=DIM_FG if dim else MUTED_FG)
        # 4. Status
        x = lay.bar_w + lay.names_w + lay.origin_w + 8
        self.status_items[r.machine_id] = c.create_text(
            x, cy, anchor="w", text=text(r.status), width=max(1, lay.status_w - 16),
            font=(UI_FONT, -lay.status_px, "bold"), fill=DIM_FG if dim else colour)
        # 5. Age
        self.age_items[r.machine_id] = c.create_text(
            width - 8, cy, anchor="e", text=text(hx_state.checked_text(_age(r.checked_age_s, elapsed))),
            font=(UI_FONT, -lay.age_px), fill=DIM_FG if dim else MUTED_FG)
        # 6. Dividers: 2 px where the line changes, 1 px between rows of one line
        if i > 0:
            same = self.rows[i - 1].line_id == r.line_id
            c.create_line(0, y0, width, y0, fill=_SEPARATOR if same else RULE_FG, width=1 if same else 2)

    def _origin_px(self, origin, family, start, room):
        """(pixel size, wraps): from the row's origin size down 1 px at a time until it fits,
        but not below 40 % of it; still too long, it wraps at that size."""
        smallest = max(8, round(start * 0.4))
        for px in range(start, smallest - 1, -1):
            if self._font(family, px, "bold").measure(origin) <= room:
                return px, False
        return smallest, True

    def _sync_flash(self):
        # A redraw in the "off" half of a flash keeps flashing bars off until the next beat
        if self.flash_on:
            return
        for r in self.rows:
            view = self.views.get(r.machine_id)
            if view is not None and view.unacked_sound and r.machine_id in self.bar_items:
                self.canvas.itemconfigure(self.bar_items[r.machine_id], fill=CARD_BG)

    def destroy(self) -> None:
        nav = getattr(self.services, "nav", None)
        super().destroy()
        _flag_changed(nav)


def _clock_text():
    return time.strftime("%I:%M %p").lstrip("0")


# --------------------------------------------------------------------------
# The alert sound and the personal pop-up
# --------------------------------------------------------------------------

class AlertSounder:
    """Rings hx_win.play_alert while sounding_alerts(...) is non-empty, including while Stripes is
    hidden in the tray, and opens the personal pop-up for alerts addressed to this user."""

    def __init__(self, services):
        self.services = services
        self.playing = False
        self.stopped = False            # stop() was called: Stripes is quitting, nothing rings again
        self.shown_ids: set[str] = set()
        self._token = services.bus.subscribe(BoardUpdate, self._on_board)

    def _on_board(self, event):
        check_tk_thread()
        self.evaluate(event.board)

    def _floor_board_open(self):
        is_open = getattr(self.services.nav, "floor_board_open", None)
        return bool(is_open()) if is_open is not None else False

    def evaluate(self, board) -> None:
        if self.stopped:
            return      # e.g. the floor board destroyed by App.destroy() after stop_services
        identity = self.services.identity
        ringing = (hx_state.sounding_alerts(board, identity, floor_board=self._floor_board_open())
                   if board is not None else [])
        if ringing and not self.playing:
            hx_win.play_alert(repeat=True)
            self.playing = True
        elif not ringing and self.playing:
            hx_win.stop_alert()
            self.playing = False
        # A new alert while it rings doesn't restart the sound; there is no snooze
        personal = [a for a in ringing
                    if any(r.startswith("user:") and same_user(r[5:], identity.user) for r in a.recipients)]
        popup = self._popup()
        if personal:
            new = {a.id for a in personal} - self.shown_ids
            if new:
                if popup is None:
                    popup = AlertPopup(self.services.nav.root, self.services)
                popup.set_alerts(personal)
                popup.show()
                self.shown_ids |= new
            elif popup is not None:
                popup.set_alerts(personal)
        elif popup is not None:
            popup.destroy()

    def _popup(self):
        window = getattr(self.services.nav, "window", None)
        popup = window("alerts") if window is not None else None
        return popup if popup is not None and _alive(popup) else None

    def stop(self) -> None:
        self.stopped = True
        if self.playing:
            hx_win.stop_alert()
            self.playing = False
        if self._token is not None:
            self.services.bus.unsubscribe(self._token)
            self._token = None
        popup = self._popup()
        if popup is not None:
            popup.destroy()


class AlertPopup(HxWindow):
    """"MapleJet alert": why this PC is ringing for the person signed in, even with Stripes in
    the tray. Shown raised and topmost for a moment, never kept on top."""
    key = "alerts"

    def __init__(self, master, services):
        super().__init__(master, services, title=N_("MapleJet alert"))
        self.geometry("520x320")
        self.minsize(420, 240)
        self.alerts = []
        self.alert_labels: list[tk.Label] = []
        body = ttk.Frame(self, padding=14)
        body.pack(fill="both", expand=True)
        heading = ttk.Label(body, style="Hx.Head.TLabel")
        self.texts.set(heading, N_("MapleJet alert"))
        heading.pack(anchor="w", pady=(0, 8))
        self.list_frame = ttk.Frame(body)
        self.list_frame.pack(fill="both", expand=True)
        note = ttk.Label(body, style="Muted.TLabel", wraplength=480)
        self.texts.set(note, N_("The sound repeats until someone acknowledges."))
        note.pack(anchor="w", pady=(8, 8))
        buttons = ttk.Frame(body)
        buttons.pack(fill="x")
        self.monitor_button = ttk.Button(buttons, command=lambda: services.nav.open_monitor())
        self.texts.set(self.monitor_button, N_("Open MapleJet monitor"))
        self.monitor_button.pack(side="left")
        self.close_button = ttk.Button(buttons, command=self.destroy)
        self.texts.set(self.close_button, N_("Close"))
        self.close_button.pack(side="left", padx=(8, 0))
        self.ack_button = ttk.Button(buttons, style="Hx.Main.TButton", command=self._acknowledge)
        self.texts.set(self.ack_button, N_("Acknowledge"))
        self.no_role_label = ttk.Label(buttons, style="Muted.TLabel", wraplength=300, justify="left")
        self.texts.set(self.no_role_label,
                       N_("Ask a Stripes MapleJet Sender or a Stripes Administrator to acknowledge it."))
        self.subscribe(DataUpdate, lambda e: self._gate())
        self._gate()

    def _gate(self):
        if roles(self.services).acknowledge:
            self.no_role_label.pack_forget()
            self.ack_button.pack(side="right")
        else:
            self.ack_button.pack_forget()
            self.no_role_label.pack(side="right")

    def set_alerts(self, alerts) -> None:
        self.alerts = list(alerts)
        self.refresh_texts()

    def refresh_texts(self) -> None:
        for child in self.list_frame.winfo_children():
            child.destroy()
        self.alert_labels = []
        for a in self.alerts:
            row = ttk.Frame(self.list_frame)
            row.pack(fill="x", pady=2)
            since = ttk.Label(row, style="Muted.TLabel", text=tr("since {time}", time=when(a.started_at)))
            since.pack(side="right", padx=(8, 0))
            label = tk.Label(row, text="♪ " + text(a.msg), anchor="w", justify="left", wraplength=340,
                             foreground=COLOUR_HEX.get(a.colour or RED, COLOUR_HEX[RED]),
                             font=(UI_FONT, 10, "bold"))
            label.pack(side="left", fill="x", expand=True)
            self.alert_labels.append(label)
        self._gate()

    def _acknowledge(self):
        ids = [a.id for a in self.alerts if not a.acked_by]
        if ids:
            self.services.watcher.acknowledge(ids)


# --------------------------------------------------------------------------
# Per-user settings
# --------------------------------------------------------------------------

class UserSettingsDialog(HxWindow):
    """"My MapleJet settings": open the floor board at startup, on which screen, at what size."""
    key = "settings"

    def __init__(self, master, services):
        super().__init__(master, services, title=N_("My MapleJet settings"))
        self.geometry("480x320")
        settings = services.settings
        body = ttk.Frame(self, padding=14)
        body.pack(fill="both", expand=True)
        heading = ttk.Label(body, style="Hx.Section.TLabel")
        self.texts.set(heading, N_("Floor board"))
        heading.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
        self.at_startup_var = tk.BooleanVar(value=settings.floor_board_at_startup)
        self.at_startup_check = ttk.Checkbutton(body, variable=self.at_startup_var)
        self.texts.set(self.at_startup_check, N_("Open the floor board when Stripes starts"))
        self.at_startup_check.grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 6))

        screen_label = ttk.Label(body)
        self.texts.set(screen_label, N_("Screen"))
        screen_label.grid(row=2, column=0, sticky="w", padx=(0, 8))
        self.screen_box = ChoiceBox(body, self._screen_choices(), width=28)
        self.screen_box.grid(row=2, column=1, sticky="w", pady=2)
        self._screen_chosen = False         # a missing screen shows as 0 but stays saved until changed
        self.screen_box.set_key(str(settings.floor_board_screen))
        if not self.screen_box.key():
            self.screen_box.set_key("0")
        self.screen_box.bind("<<ComboboxSelected>>", lambda e: setattr(self, "_screen_chosen", True), add="+")

        size_label = ttk.Label(body)
        self.texts.set(size_label, N_("Size"))
        size_label.grid(row=3, column=0, sticky="w", padx=(0, 8))
        size_row = ttk.Frame(body)
        size_row.grid(row=3, column=1, sticky="w", pady=2)
        window_size = settings.size_tuple()
        self.size_var = tk.StringVar(value="window" if window_size else "full")
        width, height = window_size or (1280, 720)
        self.width_var = tk.StringVar(value=str(width))
        self.height_var = tk.StringVar(value=str(height))
        self.full_radio = ttk.Radiobutton(size_row, variable=self.size_var, value="full", command=self._size_mode)
        self.texts.set(self.full_radio, N_("Full screen"))
        self.full_radio.pack(side="left")
        self.window_radio = ttk.Radiobutton(size_row, variable=self.size_var, value="window", command=self._size_mode)
        self.texts.set(self.window_radio, N_("Window"))
        self.window_radio.pack(side="left", padx=(10, 4))
        (wmin, wmax), (hmin, hmax) = _SIZE_LIMITS
        self.width_spin = ttk.Spinbox(size_row, from_=wmin, to=wmax, increment=10, width=6,
                                      textvariable=self.width_var)
        self.width_spin.pack(side="left")
        ttk.Label(size_row, text="x").pack(side="left", padx=3)
        self.height_spin = ttk.Spinbox(size_row, from_=hmin, to=hmax, increment=10, width=6,
                                       textvariable=self.height_var)
        self.height_spin.pack(side="left")
        self._size_mode()

        note = ttk.Label(body, style="Muted.TLabel", wraplength=440, justify="left")
        self.texts.set(note, N_("The floor board opens whenever Stripes starts on this PC, including when you sign "
                                "in to Windows."))
        note.grid(row=4, column=0, columnspan=2, sticky="w", pady=(8, 8))
        self.problem_label = ttk.Label(body, style="Bad.TLabel", wraplength=440)

        buttons = ttk.Frame(body)
        buttons.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        body.columnconfigure(1, weight=1)
        self.open_button = ttk.Button(buttons, command=self._open_now)
        self.texts.set(self.open_button, N_("Open the floor board now"))
        self.open_button.pack(side="left")
        self.save_button = ttk.Button(buttons, style="Hx.Main.TButton", command=self._save)
        self.texts.set(self.save_button, N_("Save"))
        self.save_button.pack(side="right")
        self.cancel_button = ttk.Button(buttons, command=self.destroy)
        self.texts.set(self.cancel_button, N_("Cancel"))
        self.cancel_button.pack(side="right", padx=(0, 8))
        self.bind("<Escape>", lambda e: self.destroy())

    def _screen_choices(self):
        mons = hx_win.monitors()
        if not mons:
            return [("0", N_("This screen"))]
        return [(str(i), Msg(N_("{n}: {width} x {height} (main)") if m.primary else N_("{n}: {width} x {height}"),
                             {"n": i + 1, "width": m.width, "height": m.height}))
                for i, m in enumerate(mons)]

    def _size_mode(self):
        state = "normal" if self.size_var.get() == "window" else "disabled"
        self.width_spin.configure(state=state)
        self.height_spin.configure(state=state)

    @staticmethod
    def _number(var, limits, fallback):
        try:
            n = int(round(float(var.get().strip().replace(",", "."))))
        except (ValueError, tk.TclError):
            n = fallback
        return min(max(n, limits[0]), limits[1])

    def values(self) -> HxSettings:
        """The settings as the dialog shows them; sizes are kept inside the Spinboxes' limits."""
        current = self.services.settings
        if self._screen_chosen or str(current.floor_board_screen) == self.screen_box.key():
            screen = int(self.screen_box.key() or 0)
        else:
            screen = current.floor_board_screen
        size = "full"
        if self.size_var.get() == "window":
            old = current.size_tuple() or (1280, 720)
            width = self._number(self.width_var, _SIZE_LIMITS[0], old[0])
            height = self._number(self.height_var, _SIZE_LIMITS[1], old[1])
            size = f"{width}x{height}"
        return HxSettings(floor_board_at_startup=bool(self.at_startup_var.get()), floor_board_screen=screen,
                          floor_board_size=size)

    def _save(self, then=None):
        """Settings updated in place and saved on a worker; an open floor board is opened again
        with the new screen and size."""
        services = self.services
        new = self.values()
        settings = services.settings
        moved = (new.floor_board_screen, new.floor_board_size) != (settings.floor_board_screen,
                                                                   settings.floor_board_size)
        settings.floor_board_at_startup = new.floor_board_at_startup
        settings.floor_board_screen = new.floor_board_screen
        settings.floor_board_size = new.floor_board_size
        for button in (self.save_button, self.open_button):
            _enable(button, False)

        def done(result, error):
            if not self.winfo_exists():
                return
            for button in (self.save_button, self.open_button):
                _enable(button, True)
            if error is not None:
                msg = error.msg if isinstance(error, HxError) else Msg(
                    N_("Stripes hit a problem: {error}"), {"error": f"{type(error).__name__}: {error}"})
                self.texts.msg(self.problem_label, msg)
                self.problem_label.grid(row=5, column=0, columnspan=2, sticky="w")
                return
            nav = services.nav
            floor = nav.window("floor")
            if then is not None:
                if floor is not None:
                    floor.destroy()
                then()
            elif floor is not None and moved:
                floor.destroy()
                nav.open_floor_board()
            self.destroy()

        services.bg.run(lambda: settings.save(services.local), done)

    def _open_now(self):
        self._save(then=self.services.nav.open_floor_board)
