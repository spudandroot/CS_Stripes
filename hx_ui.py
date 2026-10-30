"""The MapleJet side of Stripes: the services, the Navigator, the MapleJet tab, start-up and shut-down.

stripes.App calls start_services() before it builds its window, so every PC running Stripes
takes part in polling even when it starts hidden in the tray. The Navigator is how the MapleJet
screens open each other: the send and admin screens (hx_ui_send, hx_ui_admin) are imported
only inside the methods that open them, so they never import each other. self_check() is the
MapleJet part of the exe's --self-check (deploy.md §5).

Only the Tk thread touches widgets. Long work runs on worker threads and comes back through the
event bus, which the Tk thread pumps every 100 ms.
"""

from __future__ import annotations

import base64
import gc
import gzip
import hashlib
import hmac
import importlib
import importlib.util
import io
import os
import re
import shutil
import socket
import struct
import sys
import tempfile
import threading
import time
import tkinter as tk
import traceback
import wave
from dataclasses import dataclass, field
from tkinter import ttk

import hx_core
import hx_model
import hx_poller
import hx_protocol
import hx_send
import hx_share
import hx_state
import hx_time
import hx_ui_common
import hx_win
from hx_core import SYSTEM_CLOCK, Clock, EventBus, Msg
from hx_model import TIMINGS, BoardUpdate, DataUpdate, Identity, PollerUpdate, ShareHealth, Timings
from hx_ui_common import HxSettings, SyncBackground, Texts, check_tk_thread, open_windows, retranslate_boxes, roles
from hx_ui_monitor import AlertSounder, FloorBoard, LineBoard, MonitorWindow, UserSettingsDialog, banner_now
from i18n import N_, add_spanish

ES = {
    "Set all clocks...": "Ajustar todos los relojes...",
    "Admin...": "Administrar...",
    "My settings...": "Mi configuración...",
    "Signed in as {user}.": "Sesión iniciada como {user}.",
    "{signed_in} {can}": "{signed_in} {can}",
    "You can send, change settings, run test prints and acknowledge alerts.":
        "Puede enviar, cambiar la configuración, hacer impresiones de prueba y reconocer alertas.",
    "You can send and acknowledge alerts.": "Puede enviar y reconocer alertas.",
    "You can change settings, run test prints and acknowledge alerts.":
        "Puede cambiar la configuración, hacer impresiones de prueba y reconocer alertas.",
    "You can watch the lines. To send, ask IT to add you to {group}.":
        "Puede ver las líneas. Para enviar, pida a TI que lo agregue a {group}.",
    "Loading the MapleJet settings...": "Cargando la configuración de MapleJet...",
    "{machine}: {alert}": "{machine}: {alert}",
    "This PC is checking {n} printers.": "Esta PC está revisando {n} impresoras.",
    "This PC is checking 1 printer.": "Esta PC está revisando 1 impresora.",
    "This PC isn't checking any printers.": "Esta PC no está revisando ninguna impresora.",
    "This PC is checking {n} printers on its own while the server can't be reached.":
        "Esta PC está revisando {n} impresoras por su cuenta mientras no se puede conectar con el servidor.",
    "Open Admin... to add printers, lines and machines.": "Abra Administrar... para agregar impresoras, líneas y máquinas.",
    "{running} If you quit now, a printer may be left halfway through. Quit anyway?":
        "{running} Si sale ahora, una impresora puede quedar a medias. ¿Salir de todos modos?",
    "Quit anyway": "Salir de todos modos",
    "Keep Stripes open": "Dejar Stripes abierto",
}
add_spanish(ES)

_PUMP_MS = 100              # the bus pump, the same cadence as stripes.Background
_GC_MS = 2000               # gc.collect() on the Tk thread (architecture §7.1 rule 3)
_TICK_MS = 1000
_REFUSED_MS = 3000


# --------------------------------------------------------------------------
# Services
# --------------------------------------------------------------------------

@dataclass(kw_only=True)
class HxServices:
    version: str
    clock: Clock
    timings: Timings
    bus: EventBus
    bg: object                   # .run(work, done): stripes.Background in the app, SyncBackground in tests
    identity: Identity
    paths: hx_share.HxPaths
    store: hx_share.ShareStore   # root None when no share is configured
    local: hx_share.LocalStore
    data: hx_share.DataService
    clients: hx_protocol.ClientPool
    time: hx_time.TimeSource
    poller: hx_poller.Poller
    watcher: hx_poller.StatusWatcher
    settings: HxSettings
    nav: "Navigator"
    started: bool = field(default=False, repr=False)   # start_services ran (stop_services checks it)

    def _parts(self):
        return dict(store=self.store, data=self.data, clients=self.clients, time=self.time,
                    identity=self.identity, clock=self.clock, timings=self.timings)

    def pipeline(self) -> hx_send.SendPipeline:
        return hx_send.SendPipeline(**self._parts())

    def actions(self) -> hx_send.PrinterActions:
        return hx_send.PrinterActions(**self._parts())

    def test_run(self, plan) -> hx_send.TestPrintRun:
        return hx_send.TestPrintRun(plan=plan, **self._parts())


def make_services(root: tk.Misc, *, config_path: str | None, bg, version: str, clock: Clock = SYSTEM_CLOCK,
                  timings: Timings = TIMINGS, identity: Identity | None = None, fs=None,
                  gates=hx_protocol.PROCESS_GATES, key: bytes | None = None) -> HxServices:
    """Builds everything and starts nothing (ui.md §15.1)."""
    identity = identity or hx_win.current_identity()
    paths = hx_share.paths(config_path)
    bus = EventBus()
    store = hx_share.ShareStore(paths.share_root, identity=identity, clock=clock, fs=fs, timings=timings,
                                version=version)
    local = hx_share.LocalStore(paths, clock=clock)
    data = hx_share.DataService(store=store, local=local, bus=bus, identity=identity, clock=clock, timings=timings)
    clients = hx_protocol.ClientPool(key=key if key is not None else hx_protocol.release_key(), clock=clock,
                                     gates=gates, timings=timings)
    time_source = hx_time.TimeSource([], clock=clock)
    poller = hx_poller.Poller(store=store, data=data, clients=clients, time=time_source, bus=bus,
                              identity=identity, version=version, clock=clock, timings=timings, local=local)
    watcher = hx_poller.StatusWatcher(store=store, data=data, poller=poller, bus=bus, identity=identity,
                                      clock=clock, timings=timings)
    settings = HxSettings.load(local)      # on the Tk thread, once (storage.md §7.1 rule 5)
    nav = Navigator(root)
    services = HxServices(version=version, clock=clock, timings=timings, bus=bus, bg=bg, identity=identity,
                          paths=paths, store=store, local=local, data=data, clients=clients, time=time_source,
                          poller=poller, watcher=watcher, settings=settings, nav=nav)
    nav.services = services
    hx_ui_common.setup_styles(root)

    def time_servers(event):
        company = event.data.company
        time_source.set_hosts([h for h in (company.time_server, company.time_server_fallback) if h])
    bus.subscribe(DataUpdate, time_servers)
    return services


def start_services(root: tk.Misc, *, config_path: str | None, bg, version: str) -> HxServices:
    """make_services, then the services' threads, the bus pump and the gc timer. Returns at once
    and never touches the share on the calling thread."""
    services = make_services(root, config_path=config_path, bg=bg, version=version)

    # Long-lived workers could free a Tk object on their own thread; collect on the Tk thread only
    gc.disable()

    loops = services._loops = {}        # timer ids, so stop_services can end both loops

    def again(name, ms, func):
        try:
            loops[name] = root.after(ms, func)
        except tk.TclError:     # the root is gone
            loops.pop(name, None)

    def collect():
        gc.collect()
        again("gc", _GC_MS, collect)

    def pump():
        services.bus.pump()
        again("pump", _PUMP_MS, pump)

    again("gc", _GC_MS, collect)
    again("pump", _PUMP_MS, pump)
    nav = services.nav
    nav.sounder = AlertSounder(services)
    services.bus.subscribe(BoardUpdate, lambda e: nav.update_monitor_flag())
    services.data.start()
    services.poller.start()
    services.watcher.start()
    services.started = True
    return services


def stop_services(services: HxServices, timeout_s: float = 2.0) -> None:
    """Silence first, then stop the poller (it hands its printers over), the watcher and the data
    service together, against one deadline. Safe on services that were never started."""
    nav = services.nav
    if nav.sounder is None or not services.started:
        hx_win.stop_alert()
        return
    nav.sounder.stop()
    helpers = []
    for name, service in (("poller", services.poller), ("watcher", services.watcher), ("data", services.data)):
        t = threading.Thread(target=_stop_quietly, args=(service, timeout_s), name=f"hx-stop-{name}", daemon=True)
        t.start()
        helpers.append(t)
    deadline = time.monotonic() + timeout_s + 0.5
    for t in helpers:
        t.join(max(0.0, deadline - time.monotonic()))
    hx_win.stop_alert()
    # Stripes is quitting: the bus pump and the gc timer end here, before the root goes
    root = nav.root
    for timer in getattr(services, "_loops", {}).values():
        try:
            root.after_cancel(timer)
        except tk.TclError:
            pass


def _stop_quietly(service, timeout_s):
    try:
        service.stop(timeout_s)
    except Exception:       # quitting goes on
        traceback.print_exc()


def can_quit(parent: tk.Misc, services: HxServices) -> bool:
    """True when nothing is running; otherwise asks once, naming the first thing running."""
    r = hx_ui_common.running()
    if not r:
        return True
    return hx_ui_common.ask(parent, Msg(N_("{running} If you quit now, a printer may be left halfway through. "
                                           "Quit anyway?"), {"running": r[0]}),
                            yes=N_("Quit anyway"), no=N_("Keep Stripes open"))


def startup(root: tk.Misc, services: HxServices) -> None:
    """Opens the floor board when this user's settings say so, even when Stripes started in the tray."""
    if services.settings.floor_board_at_startup:
        root.after(1000, services.nav.open_floor_board)


def open_monitor(root: tk.Misc, services: HxServices) -> None:
    """The tray's "MapleJet monitor"."""
    services.nav.open_monitor()


def language_changed(services: HxServices) -> None:
    """Every MapleJet screen's text in the new language; bag text isn't touched."""
    tab = services.nav.tab
    if tab is not None:
        try:
            if tab.winfo_exists():
                tab.retranslate()
        except tk.TclError:
            pass
    for window in open_windows():
        window.retranslate()


# --------------------------------------------------------------------------
# The Navigator
# --------------------------------------------------------------------------

def _send_has_choices(window) -> bool:
    """Whether an open Send window has any product chosen (ui.md §5.1 item 2)."""
    has = getattr(window, "has_choices", None)
    if callable(has):
        return bool(has())
    for block in getattr(window, "blocks", {}).values():
        box = getattr(block, "product_box", None)
        chosen = getattr(box, "record_id", None) or getattr(box, "key", None)
        if callable(chosen) and chosen():
            return True
    return False


class Navigator:
    """How the screens open each other without importing each other. One window per key: opening
    a key that is open shows that window instead."""

    def __init__(self, root: tk.Misc) -> None:
        self.root = root
        self._services = None
        self.windows: dict[str, hx_ui_common.HxWindow] = {}
        self.tab = None                 # the MapleJetTab, set by its constructor
        self.tab_shown = False
        self.sounder = None             # set by start_services
        self.offline_sends: list[str] = []
        self.offline_answers: dict[str, tuple[list[str], bool]] = {}
        self._flag = None               # the last (monitor_open, floor_board) given to the poller
        self._offline_running = False
        self._share_token = None

    @property
    def services(self) -> HxServices:
        return self._services

    @services.setter
    def services(self, services) -> None:
        self._services = services
        if services is not None and self._share_token is None:
            self._share_token = services.bus.subscribe(ShareHealth, self._on_share_health)

    # windows

    def window(self, key: str):
        """The open window with that key, else None."""
        window = self.windows.get(key)
        if window is None:
            return None
        try:
            if window.winfo_exists():
                return window
        except tk.TclError:
            pass
        self.windows.pop(key, None)
        return None

    def floor_board_open(self) -> bool:
        return self.window("floor") is not None

    def _shown(self, key):
        window = self.window(key)
        if window is not None:
            window.show()
            return True
        return False

    def _allowed(self, role) -> bool:
        r = roles(self.services)
        if role == "sender" and not r.sender:
            hx_ui_common.tell(self.root, Msg(N_("Only Stripes MapleJet Senders can do this.")), kind="warning")
            return False
        if role == "admin" and not r.admin:
            hx_ui_common.tell(self.root, Msg(N_("Only Stripes Administrators can do this.")), kind="warning")
            return False
        return True

    def open_send(self, *, prefill=None, line_ids: list[str] | None = None) -> None:
        if not self._allowed("sender"):
            return
        import hx_ui_send
        window = self.window("send")
        if window is None:
            hx_ui_send.SendWindow(self.root, self.services, prefill=prefill, line_ids=line_ids)
            return
        if getattr(window, "page", "choose") != "choose":
            window.show()
            hx_ui_common.tell(window, Msg(N_("A Send is running. Wait for it to finish.")))
            return
        if prefill is not None or line_ids:
            replace = (not _send_has_choices(window)
                       or hx_ui_common.ask(window, Msg(N_("Replace the choices in the Send window?")),
                                           yes=N_("Replace them"), no=N_("Keep them")))
            if replace:
                if prefill is not None:
                    window.fill(prefill)
                else:
                    window.tick_lines(list(line_ids))
        window.show()

    def open_result(self, send_id: str) -> None:
        import hx_ui_send
        hx_ui_send.ResultWindow.from_log(self.root, self.services, send_id)

    def open_failure(self, result, plan) -> None:
        if self._shown(f"failure:{result.send_id}"):
            return
        import hx_ui_send
        hx_ui_send.FailureWindow(self.root, self.services, result, plan)

    def open_monitor(self) -> None:
        if not self._shown("monitor"):
            MonitorWindow(self.root, self.services)
        self.update_monitor_flag()

    def open_floor_board(self) -> None:
        services = self.services
        if not self._shown("floor"):
            FloorBoard(self.root, services, screen=services.settings.floor_board_screen,
                       size=services.settings.floor_board_size)
        self.update_monitor_flag()
        if self.sounder is not None:
            self.sounder.evaluate(services.watcher.latest)

    def open_admin(self, *, page: str = "", record_id: str = "") -> None:
        if not self._allowed("admin"):
            return
        window = self.window("admin")
        if window is not None:
            window.show()
            window.select(page, record_id)
            return
        import hx_ui_admin
        hx_ui_admin.AdminWindow(self.root, self.services, page=page, record_id=record_id)

    def open_test_print(self, *, film_id: str, machine_id: str = "", kind: str = "test") -> None:
        if kind != "test":
            raise ValueError(f"the test print window runs kind 'test', not {kind!r}")
        if not self._allowed("admin") or self._shown(f"test:{film_id}"):
            return
        import hx_ui_admin
        hx_ui_admin.TestPrintWindow(self.root, self.services, film_id=film_id, machine_id=machine_id, kind=kind)

    def stop(self, machine_ids: list[str], *, reason: str = "user", send_id: str = "") -> None:
        if not self._allowed("sender") or self._shown("stop:" + ",".join(sorted(machine_ids))):
            return
        import hx_ui_send
        hx_ui_send.StopDialog(self.root, self.services, machine_ids=list(machine_ids), reason=reason,
                              send_id=send_id)

    def resume(self, machine_ids: list[str]) -> None:
        if not self._allowed("sender") or self._shown("resume:" + ",".join(sorted(machine_ids))):
            return
        import hx_ui_send
        hx_ui_send.ResumeDialog(self.root, self.services, machine_ids=list(machine_ids))

    def set_clocks(self, line_ids: list[str]) -> None:
        if not self._allowed("sender") or self._shown("clocks:" + ",".join(sorted(line_ids))):
            return
        import hx_ui_send
        hx_ui_send.SetClocksDialog(self.root, self.services, line_ids=list(line_ids))

    # the monitor flag

    def update_monitor_flag(self) -> None:
        """Tell the poller whether a monitor is open here: the tab while shown in a visible main
        window, the MapleJet monitor or the floor board (states.md §13). Only changes are sent."""
        services = self.services
        if services is None:
            return
        try:
            viewable = bool(self.root.winfo_viewable())
        except tk.TclError:
            viewable = False
        floor = self.floor_board_open()
        monitor_open = bool((self.tab_shown and viewable) or self.window("monitor") is not None or floor)
        flag = (monitor_open, floor)
        if flag == self._flag:
            return
        floor_before = self._flag[1] if self._flag is not None else False
        self._flag = flag
        services.poller.set_monitor_open(monitor_open, floor_board=floor)
        if floor != floor_before and self.sounder is not None:
            self.sounder.evaluate(services.watcher.latest)

    # Sends made without the share (send.md §14.1)

    def note_offline_send(self, send_id: str) -> None:
        if send_id not in self.offline_sends:
            self.offline_sends.append(send_id)

    def note_offline_answer(self, send_id: str, machine_ids: list[str], everything_right: bool) -> None:
        check_tk_thread()
        self.offline_answers[send_id] = (list(machine_ids), bool(everything_right))
        # The share may have come back, and its run dropped this Send, while the answer was on its
        # way: keep the Send until its answer is on the share, and run now when the share is up
        # (no ShareHealth(ok=True) comes while it stays up)
        self.note_offline_send(send_id)
        services = self.services
        if services is not None and services.store.available and not self._offline_running:
            self._record_offline()

    def _on_share_health(self, event):
        check_tk_thread()
        if event.ok and self.offline_sends and not self._offline_running:
            self._record_offline()

    def _record_offline(self):
        services = self.services
        ids = list(self.offline_sends)
        answers = dict(self.offline_answers)    # the answers this run passes to confirm
        self._offline_running = True

        def work():
            done = []
            pipeline = services.pipeline()
            for send_id in ids:
                try:
                    entry = services.data.find_send(send_id)
                    if entry is None:
                        continue            # kept for the next time the share comes back
                    pipeline.record_offline(entry)
                    answer = answers.get(send_id)
                    if answer is not None:
                        pipeline.confirm(send_id, answer[0], answer[1])
                    done.append(send_id)
                except hx_model.HxError:
                    pass                    # kept for the next ShareHealth
                except Exception:
                    traceback.print_exc()
            return done

        def finished(done, error):
            self._offline_running = False
            if error is not None:
                return
            again = False
            for send_id in done or []:
                if self.offline_answers.get(send_id) != answers.get(send_id):
                    # The sender answered while this run was going (bg's done() runs later on the
                    # Tk thread): that answer isn't on the share yet, so the Send stays
                    again = True
                    continue
                if send_id in self.offline_sends:
                    self.offline_sends.remove(send_id)
                self.offline_answers.pop(send_id, None)
            if again and services.store.available:
                self._record_offline()      # record_offline again writes nothing; then confirm

        services.bg.run(work, finished)


# --------------------------------------------------------------------------
# The MapleJet tab
# --------------------------------------------------------------------------

def _footer(update: PollerUpdate | None) -> Msg:
    n = len(update.polling) if update is not None else 0
    mode = update.mode if update is not None else "idle"
    if mode == "standalone":
        return Msg(N_("This PC is checking {n} printers on its own while the server can't be reached."), {"n": n})
    if mode == "share" and n == 1:
        return Msg(N_("This PC is checking 1 printer."))
    if mode == "share" and n > 1:
        return Msg(N_("This PC is checking {n} printers."), {"n": n})
    return Msg(N_("This PC isn't checking any printers."))


def _who(services) -> Msg:
    data = services.data.current
    r = roles(services)
    if data is None:
        can = Msg(N_("Loading the MapleJet settings..."))
    elif r.sender and r.admin:
        can = Msg(N_("You can send, change settings, run test prints and acknowledge alerts."))
    elif r.sender:
        can = Msg(N_("You can send and acknowledge alerts."))
    elif r.admin:
        can = Msg(N_("You can change settings, run test prints and acknowledge alerts."))
    else:
        can = Msg(N_("You can watch the lines. To send, ask IT to add you to {group}."),
                  {"group": data.company.sender_group})
    return Msg(N_("{signed_in} {can}"), {"signed_in": Msg(N_("Signed in as {user}."),
                                                          {"user": services.identity.user}), "can": can})


class MapleJetTab(ttk.Frame):
    """The MapleJet top tab: Send and the monitors, who is signed in, the banner, the
    administrators' strip, the line cards and what this PC is checking (ui.md §4)."""
    hx_no_translate = True

    def __init__(self, master, services: HxServices):
        super().__init__(master, padding=(10, 8))
        self.services = services
        self.texts = Texts()
        self.board_view = None
        self.received = None
        self.poller_update = services.poller.last_update()
        self.roles = roles(services)
        self._tokens = []
        self._timer = None
        self._refused_timer = None
        self._refused = False
        self._destroyed = False
        nav = services.nav
        nav.tab = self

        # Row 0: the buttons
        bar = ttk.Frame(self)
        bar.grid(row=0, column=0, sticky="ew")
        self.send_button = ttk.Button(bar, style="Hx.Main.TButton", command=lambda: nav.open_send())
        self.clocks_button = ttk.Button(bar, command=self._set_all_clocks)
        self.monitor_button = ttk.Button(bar, command=lambda: nav.open_monitor())
        self.floor_button = ttk.Button(bar, command=lambda: nav.open_floor_board())
        self.admin_button = ttk.Button(bar, command=lambda: nav.open_admin())
        self.settings_button = ttk.Button(bar, command=self._open_settings)
        for button, label in ((self.send_button, N_("Send...")), (self.clocks_button, N_("Set all clocks...")),
                              (self.monitor_button, N_("MapleJet monitor")), (self.floor_button, N_("Floor board")),
                              (self.admin_button, N_("Admin...")), (self.settings_button, N_("My settings..."))):
            self.texts.set(button, label)

        # Row 1: who is signed in
        self.who_label = ttk.Label(self, wraplength=900, justify="left")
        self.who_label.grid(row=1, column=0, sticky="w", pady=(8, 4))

        # Row 2: the banner
        self.banner_frame = ttk.Frame(self)
        self.banner_label = ttk.Label(self.banner_frame, style="Warn.TLabel", wraplength=760, justify="left")
        self.banner_label.pack(side="left", fill="x", expand=True)
        self.refresh_button = ttk.Button(self.banner_frame, command=self._refresh)
        self.texts.set(self.refresh_button, N_("Refresh now"))

        # Row 3: for administrators
        self.admin_frame = ttk.Frame(self)
        self.admin_labels: list[ttk.Label] = []

        # Row 4: the line cards, or what stands in for them
        self.board = LineBoard(self, services)
        self.empty_frame = ttk.Frame(self)
        self.empty_labels: list[ttk.Label] = []

        # Row 5: what this PC is checking
        self.footer_label = ttk.Label(self, style="Muted.TLabel")
        self.footer_label.grid(row=5, column=0, sticky="w", pady=(6, 0))
        self.columnconfigure(0, weight=1)
        self.rowconfigure(4, weight=1)

        self._subscribe(BoardUpdate, self._on_board)
        self._subscribe(DataUpdate, lambda e: self._on_data())
        self._subscribe(ShareHealth, lambda e: self._on_data())
        self._subscribe(PollerUpdate, self._on_poller)
        latest = services.watcher.latest
        if latest is not None:
            self.board_view, self.received = latest, services.clock.monotonic()
        self._on_data()
        self._show_footer()
        self._timer = self.after(_TICK_MS, self._tick)

    # events

    def _subscribe(self, event_type, callback):
        def deliver(event):
            check_tk_thread()
            if not self._destroyed:
                callback(event)
        self._tokens.append(self.services.bus.subscribe(event_type, deliver))

    def _on_board(self, event):
        self.board_view = event.board
        self.received = self.services.clock.monotonic()
        self._show_banner()
        self._show_admin()
        if self.services.data.current is None:
            # No DataUpdate comes while the settings fail to load; the watcher's boards do
            self._show_board_area()

    def _on_data(self):
        self.roles = roles(self.services)
        self._apply_roles()
        self.texts.msg(self.who_label, _who(self.services))
        self._show_banner()
        self._show_admin()
        self._show_board_area()

    def _on_poller(self, event):
        self.poller_update = event
        self._show_footer()

    def _tick(self):
        self._timer = None
        if self._destroyed:
            return
        try:
            self._show_banner()
        except tk.TclError:
            return
        self._timer = self.after(_TICK_MS, self._tick)

    # parts

    def _apply_roles(self):
        """Role-gated buttons are left out, not disabled (ui.md §2.7)."""
        r = self.roles
        left = [b for b, ok in ((self.send_button, r.sender), (self.clocks_button, r.sender)) if ok]
        right = [b for b, ok in ((self.settings_button, True), (self.admin_button, r.admin),
                                 (self.floor_button, True), (self.monitor_button, True)) if ok]
        for button in (self.send_button, self.clocks_button, self.monitor_button, self.floor_button,
                       self.admin_button, self.settings_button):
            button.pack_forget()
        for button in left:
            button.pack(side="left", padx=(0, 6))
        for button in right:
            button.pack(side="right", padx=(6, 0))

    def _elapsed(self):
        if self.received is None:
            return 0.0
        return max(0.0, self.services.clock.monotonic() - self.received)

    def _show_banner(self):
        board = self.board_view
        banner = banner_now(board, self._elapsed())
        if self._refused:
            self.texts.set(self.banner_label, N_("You can refresh once every {seconds} s."),
                           seconds=int(self.services.timings.refresh_now_min_s))
        elif banner is not None:
            self.texts.msg(self.banner_label, banner)
        if banner is None and not self._refused:
            self.banner_frame.grid_forget()
            return
        if board is not None and board.standalone:
            self.refresh_button.pack(side="right", padx=(8, 0))
        else:
            self.refresh_button.pack_forget()
        self.banner_frame.grid(row=2, column=0, sticky="ew", pady=(0, 6))

    def _refresh(self):
        if not self.services.poller.refresh_now():
            self._refused = True
            if self._refused_timer is not None:
                self.after_cancel(self._refused_timer)
            self._refused_timer = self.after(_REFUSED_MS, self._end_refused)
        self._show_banner()

    def _end_refused(self):
        self._refused_timer = None
        self._refused = False
        if not self._destroyed:
            self._show_banner()

    def _admin_lines(self) -> list[Msg]:
        lines = list(self.services.data.problems)
        board = self.board_view
        if board is not None:
            for line in board.lines:
                for m in line.machines:
                    for a in m.alerts:
                        if a.active and "admins" in a.recipients:
                            lines.append(Msg(N_("{machine}: {alert}"), {"machine": m.name, "alert": a.msg}))
        return lines

    def _show_admin(self):
        for label in self.admin_labels:
            self.texts.forget(label)
            label.destroy()
        self.admin_labels = []
        lines = self._admin_lines() if self.roles.admin else []
        for msg in lines:
            label = ttk.Label(self.admin_frame, style="Bad.TLabel", wraplength=900, justify="left")
            self.texts.msg(label, msg)
            label.pack(anchor="w")
            self.admin_labels.append(label)
        if lines:
            self.admin_frame.grid(row=3, column=0, sticky="ew", pady=(0, 6))
        else:
            self.admin_frame.grid_forget()

    def _empty_texts(self):
        """What stands in for the line cards (ui.md §4.6), or None when the cards show."""
        data = self.services.data
        if data.current is None:
            if data.problems:
                return [(m, "Warn.TLabel") for m in data.problems]
            return [(Msg(N_("Loading the MapleJet settings...")), "Muted.TLabel")]
        if not any(line.active for line in data.current.lines.values()):
            out = [(Msg(N_("No lines are set up yet.")), "TLabel")]
            if self.roles.admin:
                out.append((Msg(N_("Open Admin... to add printers, lines and machines.")), "Muted.TLabel"))
            return out
        return None

    def _show_board_area(self):
        for label in self.empty_labels:
            self.texts.forget(label)
            label.destroy()
        self.empty_labels = []
        empty = self._empty_texts()
        if empty is None:
            self.empty_frame.grid_forget()
            self.board.grid(row=4, column=0, sticky="nsew")
            return
        self.board.grid_forget()
        for msg, style in empty:
            label = ttk.Label(self.empty_frame, style=style, wraplength=900, justify="left")
            self.texts.msg(label, msg)
            label.pack(anchor="w", pady=2)
            self.empty_labels.append(label)
        self.empty_frame.grid(row=4, column=0, sticky="nsew", pady=(12, 0))

    def _show_footer(self):
        self.texts.msg(self.footer_label, _footer(self.poller_update))

    def _set_all_clocks(self):
        data = self.services.data.current
        if data is None:
            return
        lines = sorted((ln for ln in data.lines.values() if ln.active),
                       key=lambda ln: (hx_state.natural_key(ln.name), ln.id))
        if lines:
            self.services.nav.set_clocks([ln.id for ln in lines])

    def _open_settings(self):
        nav = self.services.nav
        window = nav.window("settings")
        if window is not None:
            window.show()
        else:
            UserSettingsDialog(nav.root, self.services)

    # shown and hidden, language, destroy

    def on_shown(self) -> None:
        nav = self.services.nav
        nav.tab_shown = True
        nav.update_monitor_flag()
        latest = self.services.watcher.latest
        if latest is not None and latest is not self.board.board:
            self.board.show(latest)
        self.board.update_ages()
        self.board.start_tick()

    def on_hidden(self) -> None:
        nav = self.services.nav
        nav.tab_shown = False
        nav.update_monitor_flag()
        self.board.stop_tick()

    def retranslate(self) -> None:
        """The tab's text in the current language; bag text (the cards' origins) isn't touched."""
        self.texts.apply()
        retranslate_boxes(self)
        self.board.retranslate()
        self._show_banner()

    def destroy(self) -> None:
        if not self._destroyed:
            self._destroyed = True
            for token in self._tokens:
                self.services.bus.unsubscribe(token)
            self._tokens.clear()
            for timer in (self._timer, self._refused_timer):
                if timer is not None:
                    try:
                        self.after_cancel(timer)
                    except tk.TclError:
                        pass
            self._timer = self._refused_timer = None
            if self.services.nav.tab is self:
                self.services.nav.tab = None
        super().destroy()


# --------------------------------------------------------------------------
# Self-check (deploy.md §1.4, §5)
# --------------------------------------------------------------------------

_SELF_CHECK_MODULES = ("hx_core", "hx_model", "hx_message", "hx_state", "hx_win", "hx_protocol", "hx_time",
                       "hx_share", "hx_send", "hx_poller", "hx_ui_common", "hx_ui_send", "hx_ui_monitor",
                       "hx_ui_admin", "hx_ui")
_SELF_CHECK_UTC = 1_793_275_200.0       # 29 Oct 2026 12:00 UTC
_SOCKET_TIMEOUT_S = 5.0
_KNOWN = (
    (lambda: hashlib.md5(b"123321").hexdigest(), "c8837b23ff8aaa8a2dde915473ce0991"),
    (lambda: hx_protocol.make_token("123321"), "Yzg4MzdiMjNmZjhhYWE4YTJkZGU5MTU0NzNjZTA5OTE="),
    (lambda: hx_protocol.password_md5(hx_protocol.make_token("123321")), "c8837b23ff8aaa8a2dde915473ce0991"),
    (lambda: hashlib.sha256(b"abc").hexdigest(), "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"),
    (lambda: hmac.new(b"Jefe", b"what do ya want for nothing?", hashlib.sha256).hexdigest(),
     "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843"),
)
# deploy.md §5.7: an all-white 1-bit 1024 x 160 BMP with the header the printer sends
_TEST_BMP = (b"BM" + struct.pack("<IHHI", 20542, 0, 0, 62)
             + struct.pack("<IiiHHIIiiII", 40, 1024, 160, 1, 1, 0, 20480, 0, 0, 2, 0)
             + bytes([0, 0, 0, 0, 255, 255, 255, 0]) + b"\xff" * 20480)
_AUTOPILOT_REPLY = (b"HTTP/1.1 200 OK\r\nServer: MapJet/v1.1\r\nConnection: close\r\nContent-Length: 4\r\n"
                    b"Content-Type: text/plain\r\n\r\n1109")
_LOGIN_PAGE = b"<!DOCTYPE html><html><body>Stripes self-check</body></html>"


def _need(condition, what):
    if not condition:
        raise RuntimeError(what)


def _frozen():
    return bool(getattr(sys, "frozen", False))


def _check_modules(root):
    for name in _SELF_CHECK_MODULES:
        importlib.import_module(name)
    if _frozen():
        _need(importlib.util.find_spec("hx_fake") is None, "the fake printer is in the exe")


def _check_hashes(root):
    for compute, expected in _KNOWN:
        got = compute()
        _need(got == expected, f"known answer {expected[:8]}... got {got!r}")


def _check_key(root):
    key = hx_protocol.release_key()
    token = hx_protocol.make_token("123321")
    stored = hx_protocol.scramble(token, key)
    try:
        import hx_key  # noqa: F401  written by release builds only
        keyed = True
    except ImportError:
        keyed = False
    _need(len(key) >= 16, "the key is too short")
    _need(hx_protocol.unscramble(stored, key) == token, "scramble didn't round-trip")
    _need(stored.startswith(f"hx1:{hx_protocol.key_id(key)}:"), "the scrambled token doesn't name its key")
    _need(hx_protocol.is_development_key(key) == (not keyed), "the key doesn't match the build")


def _wake(address, kind):
    """Unblock a self-check server still waiting after its client gave up."""
    try:
        with socket.socket(socket.AF_INET, kind) as s:
            s.settimeout(1.0)
            if kind == socket.SOCK_STREAM:
                s.connect(address)
            else:
                s.sendto(bytes(48), address)
    except OSError:
        pass


def _check_http(root):
    server = socket.create_server(("127.0.0.1", 0))
    server.settimeout(_SOCKET_TIMEOUT_S)
    port = server.getsockname()[1]
    gz = gzip.compress(_LOGIN_PAGE, mtime=0)
    login_reply = (b"HTTP/1.1 200 OK\r\nServer: MapJet/v1.1\r\nConnection: close\r\nContent-Type: text/html\r\n"
                   b"Content-Encoding: gzip\r\nSet-Cookie: Access_token_1109=a1b2\r\n"
                   b"Set-Cookie: Auth_token_1109=c3d4\r\nContent-Length: %d\r\n\r\n" % len(gz)) + gz
    received = []

    def serve():
        try:
            for reply in (_AUTOPILOT_REPLY, login_reply):
                conn, _ = server.accept()
                with conn:
                    conn.settimeout(_SOCKET_TIMEOUT_S)
                    data = b""
                    while b"\r\n\r\n" not in data:
                        chunk = conn.recv(65536)
                        if not chunk:
                            break
                        data += chunk
                    head, _, body = data.partition(b"\r\n\r\n")
                    found = re.search(rb"(?im)^content-length:\s*(\d+)\s*$", head)
                    length = int(found.group(1)) if found else 0
                    while len(body) < length:
                        chunk = conn.recv(65536)
                        if not chunk:
                            break
                        body += chunk
                    received.append(head + b"\r\n\r\n" + body)
                    conn.sendall(reply)
        except OSError:
            pass
        finally:
            server.close()

    thread = threading.Thread(target=serve, name="hx-selfcheck-http", daemon=True)
    thread.start()
    try:
        token = hx_protocol.make_token("123321")
        req1 = hx_protocol.autopilot_request(1109, token, "<HXUI/>")
        r1 = hx_protocol.http_exchange("127.0.0.1", port, req1, timeout_s=2.0,
                                       max_body=hx_protocol.MAX_AUTOPILOT_REPLY, label="self-check")
        _need(r1.status == 200, f"Autopilot status {r1.status}")
        _need(hx_protocol.check_reply("HXUI", r1.body.decode("ascii").strip()) == 1109, "Autopilot reply")
        req2 = hx_protocol.login_request("127.0.0.1", port, hx_protocol.password_md5(token))
        r2 = hx_protocol.http_exchange("127.0.0.1", port, req2, timeout_s=2.0, max_body=hx_protocol.MAX_LOGIN_REPLY,
                                       label="self-check", allow_cut_short=True)
        _need(r2.status == 200, f"login status {r2.status}")
        _need(r2.body == _LOGIN_PAGE, "the gzipped login page")
        _need(r2.cookies() == {"Access_token_1109": "a1b2", "Auth_token_1109": "c3d4"}, "the login cookies")
    finally:
        if thread.is_alive() and len(received) < 2:
            _wake(("127.0.0.1", port), socket.SOCK_STREAM)
        thread.join(_SOCKET_TIMEOUT_S)
    _need(len(received) == 2 and received[0] == req1, "the Autopilot request didn't arrive intact")
    _need(received[1].endswith(b"pass=c8837b23ff8aaa8a2dde915473ce0991&remember=on"), "the login request")


def _check_sntp(root):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(_SOCKET_TIMEOUT_S)
    port = sock.getsockname()[1]
    requests = []

    def serve():
        try:
            request, address = sock.recvfrom(1024)
            requests.append(request)
            stamp = struct.pack("!II", int(_SELF_CHECK_UTC) + hx_time.NTP_EPOCH_OFFSET, 0)
            reply = bytearray(48)
            reply[0], reply[1] = 0x24, 2
            reply[24:32] = request[40:48].ljust(8, b"\0")
            reply[32:40] = stamp
            reply[40:48] = stamp
            sock.sendto(bytes(reply), address)
        except OSError:
            pass
        finally:
            sock.close()

    thread = threading.Thread(target=serve, name="hx-selfcheck-sntp", daemon=True)
    thread.start()
    try:
        utc = hx_time.sntp_query("127.0.0.1", port=port, timeout_s=2.0)
    finally:
        if thread.is_alive() and not requests:
            _wake(("127.0.0.1", port), socket.SOCK_DGRAM)
        thread.join(_SOCKET_TIMEOUT_S)
    _need(abs(utc - _SELF_CHECK_UTC) < 1.0, f"SNTP time {utc!r}")
    _need(len(requests) == 1 and len(requests[0]) == 48 and requests[0][0] == 0x23, "the SNTP request")
    offset = hx_time.local_utc_offset(time.time())
    _need(isinstance(offset, float) and -50_400 <= offset <= 50_400, f"UTC offset {offset!r}")


def _check_files(root):
    hx_share.self_check()
    folder = tempfile.mkdtemp(prefix="stripes-hx-swap-")
    try:
        a, b = os.path.join(folder, "a.json"), os.path.join(folder, "b.json")
        with open(a, "wb") as f:
            f.write(b"old")
        h = hx_win.open_for_read(a)
        try:
            with open(b, "wb") as f:
                f.write(b"new")
            os.replace(b, a)            # must work while a reader holds the file open
            _need(h.read() == b"old", "the open reader didn't keep the old file")
        finally:
            h.close()
        with open(a, "rb") as f:
            _need(f.read() == b"new", "the swap didn't replace the file")
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def _check_windows(root):
    hx_win.self_check()
    if hx_win.is_windows():
        ident = hx_win.current_identity()
        sid = hx_win.lookup_group_sid(ident.user)
        _need("\\" in ident.user, "the user name has no domain")
        _need("S-1-1-0" in ident.sids, "the sign-in token's groups weren't read")
        _need(sid is not None and re.fullmatch(r"S-1-5-21(-\d+){4}", sid), f"the user's SID {sid!r}")
        _need(isinstance(hx_win.monitors(), list), "monitors() didn't return a list")


def _check_sound(root):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(hx_win.__file__)))
    path = os.path.join(base, "assets", "alert.wav")
    with wave.open(path, "rb") as w:
        shape = (w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes())
    _need(shape == (22050, 1, 2, 66150), f"alert.wav is {shape}")
    if hx_win.is_windows():
        import winsound
        for name in ("SND_FILENAME", "SND_ASYNC", "SND_LOOP"):
            _need(hasattr(winsound, name), f"winsound.{name}")
        hx_win.stop_alert()


def _check_pictures(root):
    from PIL import Image, ImageTk
    bmp = hx_protocol.decode_preview(hx_protocol.PREVIEW_PREFIX + " " + base64.b64encode(_TEST_BMP).decode())
    img = hx_ui_common.bmp_to_image(bmp)
    photo = ImageTk.PhotoImage(img, master=root)
    _need(bmp == _TEST_BMP and img.size == (1024, 160), "the preview didn't decode")
    del photo
    top = tk.Toplevel(root)
    try:
        top.withdraw()
        out = io.BytesIO()
        Image.new("1", (1024, 160), 1).save(out, "BMP")
        view = hx_ui_common.PictureView(top)
        view.set_bmp(out.getvalue())
        _need(view.bmp == out.getvalue(), "PictureView didn't keep its picture")
        bag = hx_ui_common.BagText(top)
        bag.set("Product of USA and Canada")
        _need(bag.get() == "Product of USA and Canada", "BagText changed its text")
    finally:
        top.destroy()
    folder = tempfile.mkdtemp(prefix="stripes-hx-picture-")
    try:
        path = os.path.join(folder, "photo.jpg")
        Image.new("RGB", (64, 64), (200, 120, 40)).save(path, "JPEG")
        png = hx_ui_common.png_preview(path)
        _need(png is not None and png.startswith(b"\x89PNG\r\n\x1a\n"), "png_preview")
        hx_ui_common.photo_from_png(root, png)
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def _check_tab(root):
    tmp = tempfile.mkdtemp(prefix="stripes-hx-tab-")
    names = ("STRIPES_V2_ROAMING", "STRIPES_V2_LOCAL", "STRIPES_V2_MACHINE", "STRIPES_V2")
    saved = {name: os.environ.get(name) for name in names}
    try:
        os.environ["STRIPES_V2_ROAMING"] = os.path.join(tmp, "roaming")
        os.environ["STRIPES_V2_LOCAL"] = os.path.join(tmp, "local")
        os.environ["STRIPES_V2_MACHINE"] = os.path.join(tmp, "machine")
        os.environ.pop("STRIPES_V2", None)      # so no share can be found
        services = make_services(root, config_path=None, bg=SyncBackground(), version="self-check",
                                 clock=hx_core.FakeClock(),
                                 identity=Identity(user="SELFCHECK\\stripes", computer="SELFCHECK",
                                                   sids=frozenset(), pid=os.getpid()),
                                 key=hx_protocol.release_key())
        tab = MapleJetTab(root, services)
        root.update_idletasks()
        tab.destroy()
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        shutil.rmtree(tmp, ignore_errors=True)


_CHECKS = (("modules", _check_modules), ("hashes", _check_hashes), ("key", _check_key), ("http", _check_http),
           ("sntp", _check_sntp), ("files", _check_files), ("windows", _check_windows), ("sound", _check_sound),
           ("pictures", _check_pictures), ("tab", _check_tab))


def self_check(root: tk.Misc) -> None:
    """The MapleJet checks of the exe's --self-check, in order (deploy.md §5.2). Talks only to
    servers it starts on 127.0.0.1, plays no sound and leaves no files; raises on the first failure."""
    for name, check in _CHECKS:
        try:
            check(root)
        except Exception as e:
            raise RuntimeError(f"hx {name}: {e!r}") from e
