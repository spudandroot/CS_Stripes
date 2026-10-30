"""Tests for hx_ui and hx_ui_monitor (the MapleJet tab, services, line cards, monitor, floor board and
alert sound). Run: xvfb-run -a python -m unittest -v

The Tk tests need a display; they skip themselves when Tk can't start. The hx_ui_common tests are
in test_hx_ui_common.py; the Send and admin screens are tested in test_hx_ui_send.py and
test_hx_ui_admin.py with a stub Navigator, so RealWindowTests here checks the real Navigator with
their real windows.
"""

import contextlib
import dataclasses
import functools
import gc
import importlib.util
import io
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import tkinter as tk
import unittest
import wave
from unittest import mock

import hx_state
import hx_ui
import hx_ui_admin
import hx_ui_common
import hx_ui_monitor
import hx_ui_send
import hx_win
import i18n
from hx_core import FakeClock, Msg
from hx_fake import FakeWorld
from hx_message import merge_plan
from hx_model import (ALLOWED_WRITE_TAGS, AMBER, BLUE, GREEN, GREY, RED, STEP_VERIFY_IDLE, TIMINGS, Alert,
                      BoardUpdate, BoardView, LineChoice, LineView, MachineResult, MachineView, PollerUpdate, PollStatus,
                      SendRequest, SendResult, ShareHealth, ShareUnavailable, new_id)
from hx_protocol import GateRegistry, release_key
from hx_ui_common import CARD_BG, COLOUR_HEX, DIM_FG, SyncBackground, WorkFailed
from hx_ui_monitor import FloorBoard, LineBoard, MonitorWindow, UserSettingsDialog, board_summary, floor_layout

HERE = os.path.dirname(os.path.abspath(__file__))

# ui.md §17.1's scaled timings, with printer and share timeouts that leave room for a busy machine. The
# lock's stale time stays well above twice the longest printer call, as the lock budget needs (storage.md
# §11.3), so a Send in RealWindowTests never judges its lock lost on a slow machine
SCALED = dataclasses.replace(TIMINGS.scaled(0.05), http_timeout_s=0.5, web_repeat_window_s=0.5, share_timeout_s=1.0,
                             lock_stale_s=30.0)


# --------------------------------------------------------------------------
# Hand-made boards (ui.md §17.1 item 6)
# --------------------------------------------------------------------------

def alert(aid, *, to=("user:EXAMPLE\\jsmith",), sound=True, acked_by="", colour=RED, text="Send failed on {machine}",
          machine="mc_a", line="ln_1", started_at=1_793_275_200.0):
    return Alert(id=aid, machine_id=machine, line_id=line, kind="send_failed", sound=sound,
                 msg=Msg(text, {"machine": machine}), started_at=started_at, recipients=list(to), colour=colour,
                 acked_by=acked_by)


def view(mid, *, line="ln_1", colour=GREEN, code="printing", headline="Printing", origin="Product of USA", age=2.0,
         monitored=True, alerts=(), details=(), **kw):
    alerts = list(alerts)
    return MachineView(machine_id=mid, line_id=line, name=kw.pop("name", f"Line 1 Bagger {mid[-1]}"), colour=colour,
                       code=code, headline=Msg(headline), details=list(details), origin_line=origin,
                       checked_age_s=age, monitored=monitored, printing=kw.pop("printing", True), alerts=alerts,
                       unacked_sound=any(a.sound and not a.acked_by for a in alerts), **kw)


def line(lid, name, machines, *, headline="Printing", colour=None):
    ages = [m.checked_age_s for m in machines if m.checked_age_s is not None]
    return LineView(line_id=lid, name=name, colour=colour or hx_state.worst([m.colour for m in machines]),
                    headline=Msg(headline), machines=list(machines),
                    monitored=bool(machines) and all(m.monitored for m in machines),
                    checked_age_s=max(ages) if ages else None)


def board(*lines, standalone=False, share_ok=True, next_check_s=None):
    return BoardView(lines=list(lines), share_ok=share_ok, standalone=standalone, next_check_s=next_check_s,
                     generated_at=1_793_275_200.0)


class DeferredBackground:
    """Like stripes.Background: work() runs at once (on its thread there), done() later, when the
    Tk thread gets round to it (finish())."""

    def __init__(self):
        self.pending = []

    def run(self, work, done):
        try:
            result, error = work(), None
        except Exception as e:
            result, error = None, e
        self.pending.append((done, result, error))

    def finish(self):
        while self.pending:
            done, result, error = self.pending.pop(0)
            done(result, error)


def no_active_lines(services):
    """The loaded settings with every line set Not active."""
    data = services.data.current
    services.data.current = dataclasses.replace(data, lines={
        k: dataclasses.replace(v, active=False) for k, v in data.lines.items()})


def shown(widget):
    """Packed, gridded or on a canvas (the root is withdrawn, so nothing is ever mapped)."""
    return widget.winfo_manager() != ""


# --------------------------------------------------------------------------
# No Tk needed
# --------------------------------------------------------------------------

class PureTests(unittest.TestCase):
    def tearDown(self):
        i18n.set_language("en")

    def test_floor_layout(self):
        lay = floor_layout(1920, 1080, 12)
        self.assertEqual((lay.header_h, lay.origin_px, lay.status_px), (76, 42, 25))
        self.assertAlmostEqual(lay.row_h, 83.67, places=2)
        self.assertEqual(floor_layout(1920, 1080, 2).row_h, 251.0)       # never taller than a quarter
        self.assertAlmostEqual(floor_layout(1920, 1080, 20).row_h, 50.2)  # beyond 12 they keep shrinking
        self.assertEqual(lay.bar_w + lay.names_w + lay.origin_w + lay.status_w + lay.age_w, 1920)
        self.assertEqual((lay.width, lay.bar_w, lay.names_w, lay.origin_w, lay.status_w, lay.age_w),
                         (1920, 48, 269, 845, 518, 240))
        # 12 machines under 4 line headers, each band half a row: all fit, filling the room under the header
        lay = floor_layout(1920, 1080, 12, 4)
        self.assertAlmostEqual(lay.row_h, 71.71, places=2)
        self.assertAlmostEqual(lay.group_h, 35.86, places=2)
        self.assertEqual((lay.rows_h, lay.scrolls, lay.origin_px, lay.status_px, lay.machine_px, lay.line_px),
                         (1004, False, 36, 22, 22, 22))
        lay = floor_layout(1280, 800, 6, 2, scrollbar_w=15)
        self.assertEqual((lay.header_h, lay.rows_h, lay.scrolls, lay.width), (56, 744, False, 1280))
        self.assertAlmostEqual(lay.row_h, 106.29, places=2)
        self.assertEqual((lay.bar_w, lay.names_w, lay.origin_w, lay.status_w, lay.age_w), (32, 179, 563, 346, 160))
        # Rows stop shrinking at FLOOR_MIN_ROW_PX: the board scrolls, its rows narrower by the scrollbar
        lay = floor_layout(1280, 800, 30, 5, scrollbar_w=15)
        self.assertEqual((lay.row_h, lay.group_h, lay.rows_h, lay.scrolls, lay.width),
                         (hx_ui_monitor.FLOOR_MIN_ROW_PX, 22.0, 1430, True, 1265))
        self.assertEqual(lay.bar_w + lay.names_w + lay.origin_w + lay.status_w + lay.age_w, 1265)
        small = floor_layout(640, 360, 40)
        self.assertTrue(small.scrolls)
        self.assertEqual((small.header_h, small.header_px, small.line_px, small.machine_px, small.origin_px,
                          small.status_px, small.age_px), (48, 22, 13, 13, 22, 13, 11))
        self.assertEqual(floor_layout(800, 600, 0).rows_h, 0)

    def test_fit_px(self):
        def measure(px, t):
            return px * len(t) / 2
        fit = hx_ui_monitor.fit_px
        # One size for all: the largest at which the widest fits
        self.assertEqual(fit(["abcd", "abcdefgh"], measure, 40, 8, 100), 25)
        self.assertEqual(fit(["abcd"], measure, 40, 8, 100), 40)                  # fits at the start size
        self.assertEqual(fit(["abcdefgh" * 10], measure, 40, 8, 100), 8)          # never below smallest
        self.assertEqual(fit(["abcdefgh" * 10], measure, 40, 1, 100), 2)          # as far as needed
        self.assertEqual(fit(["", ""], measure, 40, 8, 1), 40)                    # nothing to fit
        self.assertEqual(fit(["abcd", "abcdefgh"], measure, 7, 8, 1000), 7)       # smallest above start
        calls = []
        fit(["abcdefgh"] * 20, lambda px, t: calls.append(px) or measure(px, t), 120, 1, 100)
        self.assertLess(len(calls), 10)                                            # each text and size once

    def test_ellipsize(self):
        ellipsize = hx_ui_monitor.ellipsize
        self.assertEqual(ellipsize("Send not confirmed", len, 100), "Send not confirmed")
        self.assertEqual(ellipsize("Send not confirmed", len, 18), "Send not confirmed")
        self.assertEqual(ellipsize("Send not confirmed", len, 8), "Send no…")
        self.assertEqual(ellipsize("Send not confirmed", len, 5), "Send…")         # no space before the ellipsis
        self.assertEqual(ellipsize("Send not confirmed", len, 1), "…")
        self.assertEqual(ellipsize("Send not confirmed", len, 0), "")

    def test_short_name(self):
        short = hx_ui_monitor.short_name
        self.assertEqual(short("Line 1 Bagger 1", "Line 1"), "Bagger 1")
        self.assertEqual(short("line 1 - Bagger 2", "Line 1"), "Bagger 2")       # any case, separators dropped
        self.assertEqual(short("Line 10 Bagger 1", "Line 1"), "Line 10 Bagger 1")  # a whole word only
        self.assertEqual(short("Bagger 3", "Line 1"), "Bagger 3")
        self.assertEqual(short("Line 1", "Line 1", "B1"), "B1")                   # nothing left: the printed code
        self.assertEqual(short("Line 1", "Line 1"), "Line 1")
        self.assertEqual(short("Line 1 Bagger 1", ""), "Line 1 Bagger 1")

    def test_board_summary(self):
        b = board(line("ln_1", "Line 1", [view("mc_1", colour=RED), view("mc_2"), view("mc_3", colour=BLUE)]),
                  line("ln_2", "Line 2", [view("mc_4", colour=GREY), view("mc_5"), view("mc_6", colour=RED)]))
        self.assertEqual([hx_ui_common.text(m) for m in board_summary(b)],
                         ["2 red", "1 in progress", "1 grey", "2 green"])
        i18n.set_language("es")
        self.assertEqual(hx_ui_common.text(board_summary(b)[0]), "2 en rojo")
        self.assertEqual(board_summary(board()), [])

    def test_banner_counts_down(self):
        b = board(standalone=True, share_ok=False, next_check_s=18.0)
        self.assertEqual(hx_ui_common.text(hx_ui_monitor.banner_now(b, 5.2)),
                         "Server unreachable: showing this PC's own checks · next check in 12 s")
        self.assertIn("in 0 s", hx_ui_common.text(hx_ui_monitor.banner_now(b, 60.0)))
        self.assertIsNone(hx_ui_monitor.banner_now(board(), 3.0))
        self.assertIsNone(hx_ui_monitor.banner_now(None))
        self.assertEqual(hx_ui_common.text(hx_ui_monitor.banner_now(board(share_ok=False))),
                         "Server unreachable: this PC isn't checking any printers.")

    def test_footer_texts(self):
        texts = [hx_ui_common.text(hx_ui._footer(u)) for u in (
            PollerUpdate(mode="share", polling=["pr_1", "pr_2"]), PollerUpdate(mode="share", polling=["pr_1"]),
            PollerUpdate(mode="share"), PollerUpdate(mode="standalone", polling=["pr_1", "pr_2", "pr_3"]),
            PollerUpdate(mode="idle"), None)]
        self.assertEqual(texts, ["This PC is checking 2 printers.", "This PC is checking 1 printer.",
                                 "This PC isn't checking any printers.",
                                 "This PC is checking 3 printers on its own while the server can't be reached.",
                                 "This PC isn't checking any printers.", "This PC isn't checking any printers."])

    def test_alert_wav(self):
        with wave.open(os.path.join(HERE, "assets", "alert.wav"), "rb") as w:
            shape = (w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes())
            got = w.readframes(w.getnframes())
        self.assertEqual(shape, (22050, 1, 2, 66150))
        with wave.open(io.BytesIO(hx_ui_common.alert_wav_bytes()), "rb") as w:
            want = w.readframes(w.getnframes())
        a = memoryview(got).cast("h")
        b = memoryview(want).cast("h")
        self.assertEqual(len(a), len(b))
        self.assertLessEqual(max(abs(x - y) for x, y in zip(a, b)), 1)

    def test_can_quit(self):
        with mock.patch.object(hx_ui_common, "ask", return_value=False) as ask:
            self.assertTrue(hx_ui.can_quit(None, None))
            ask.assert_not_called()
            hx_ui_common.mark_running("send:sd_test", Msg("A Send to {machines} is still running.",
                                                          {"machines": "Line 1 Bagger 1"}))
            try:
                self.assertFalse(hx_ui.can_quit(None, None))
                self.assertEqual(ask.call_count, 1)
                msg = ask.call_args[0][1]
                self.assertEqual(msg.values["running"].values["machines"], "Line 1 Bagger 1")
                self.assertEqual(ask.call_args[1], {"yes": "Quit anyway", "no": "Keep Stripes open"})
                ask.return_value = True
                self.assertTrue(hx_ui.can_quit(None, None))
            finally:
                hx_ui_common.mark_done("send:sd_test")
        self.assertTrue(hx_ui.can_quit(None, None))


# --------------------------------------------------------------------------
# Tk
# --------------------------------------------------------------------------

class UiCase(unittest.TestCase):
    """ui.md §17.1: a FakeWorld, the v2 folders in temp folders, services from make_services, strict
    threads, and tell/ask patched on hx_ui_common.

    As in the app (architecture §7.1 rule 3), garbage is collected on the Tk thread only: the
    collector is off for the whole class and runs after each test. Otherwise a worker thread that
    happens to start a collection frees a window's leftover Tk fonts off the Tk thread, and Tk hangs."""
    root = None
    _gc_was_enabled = True

    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
        except tk.TclError as e:
            raise unittest.SkipTest(f"Tk can't start: {e}")
        cls.root.withdraw()
        cls._gc_was_enabled = gc.isenabled()
        gc.disable()

    @classmethod
    def tearDownClass(cls):
        for window in hx_ui_common.open_windows():
            window.destroy()
        gc.collect()                    # the last test's leftovers, while Tk is still there
        cls.root.destroy()
        gc.collect()
        if cls._gc_was_enabled:
            gc.enable()

    def setUp(self):
        self.addCleanup(gc.collect)     # registered first, so it runs last: after every other cleanup
        self.tmp = tempfile.mkdtemp(prefix="hx_ui_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.world = FakeWorld(self.tmp, printers=[{}], timings=SCALED)
        self.addCleanup(self.world.close)
        env = {"STRIPES_V2": self.world.share_root}
        for name, folder in (("STRIPES_V2_ROAMING", "roaming"), ("STRIPES_V2_LOCAL", "local"),
                             ("STRIPES_V2_MACHINE", "machine")):
            env[name] = os.path.join(self.tmp, "pc", folder)
            os.makedirs(env[name])
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        hx_ui_common.STRICT_THREADS = True
        self.tell = self.patch(hx_ui_common, "tell")
        self.ask = self.patch(hx_ui_common, "ask", return_value=True)

    def tearDown(self):
        hx_ui_common.STRICT_THREADS = False
        i18n.set_language("en")
        for window in hx_ui_common.open_windows():
            window.destroy()
        for child in self.root.winfo_children():
            child.destroy()

    def patch(self, target, name, **kw):
        patcher = mock.patch.object(target, name, **kw)
        found = patcher.start()
        self.addCleanup(patcher.stop)
        return found

    def make(self, name="jsmith", *, admin=True, sender=True, load=True):
        identity = self.world.identity(name, admin=admin, sender=sender)
        services = hx_ui.make_services(self.root, config_path=None, bg=SyncBackground(), version="test",
                                       timings=SCALED, identity=identity, fs=self.world.fs.view(), gates=GateRegistry(),
                                       key=release_key())
        if load:
            services.data.run_once()
            services.bus.pump()
        self.services = services
        return services

    def pump(self, until, timeout_s=10):
        deadline = time.monotonic() + timeout_s
        while not until():
            if time.monotonic() > deadline:
                self.fail("timed out")
            self.root.update()
            self.services.bus.pump()
            time.sleep(0.01)

    def labels(self, frame):
        return [w.cget("text") for w in frame.winfo_children() if shown(w)]


class TabTests(UiCase):
    def test_tab_builds_and_gates_roles(self):
        cases = (("akim", False, False, "Signed in as EXAMPLE\\akim. You can watch the lines. To send, ask IT to add "
                                        "you to Stripes MapleJet Senders."),
                 ("jsmith", False, True, "Signed in as EXAMPLE\\jsmith. You can send and acknowledge alerts."),
                 ("akim", True, False, "Signed in as EXAMPLE\\akim. You can change settings, run test prints and "
                                        "acknowledge alerts."),
                 ("jsmith", True, True, "Signed in as EXAMPLE\\jsmith. You can send, change settings, run test prints "
                                      "and acknowledge alerts."))
        for name, admin, sender, who in cases:
            with self.subTest(name=name):
                services = self.make(name, admin=admin, sender=sender)
                tab = hx_ui.MapleJetTab(self.root, services)
                self.assertIs(services.nav.tab, tab)
                self.assertEqual((shown(tab.send_button), shown(tab.clocks_button)), (sender, sender))
                self.assertEqual(shown(tab.admin_button), admin)
                for button in (tab.monitor_button, tab.floor_button, tab.settings_button):
                    self.assertTrue(shown(button))
                self.assertEqual(tab.who_label.cget("text"), who)
                self.assertTrue(shown(tab.board))                       # Line 1 is set up
                self.assertFalse(shown(tab.banner_frame))
                self.assertEqual(tab.footer_label.cget("text"), "This PC isn't checking any printers.")
                tab.destroy()
                self.assertIsNone(services.nav.tab)
        # Before the settings are loaded
        services = self.make(load=False)
        tab = hx_ui.MapleJetTab(self.root, services)
        self.assertEqual(tab.who_label.cget("text"), "Signed in as EXAMPLE\\jsmith. Loading the MapleJet settings...")
        self.assertFalse(shown(tab.send_button) or shown(tab.admin_button))
        self.assertFalse(shown(tab.board))
        self.assertEqual(self.labels(tab.empty_frame), ["Loading the MapleJet settings..."])
        # The roles follow the next DataUpdate
        services.data.run_once()
        services.bus.pump()
        self.assertTrue(shown(tab.send_button) and shown(tab.admin_button) and shown(tab.board))
        self.assertIn("You can send, change settings", tab.who_label.cget("text"))

    def test_tab_states_and_strip(self):
        # No share at all: the problem, to everyone
        services = self.make("akim", sender=False)
        services.data.current, services.data.problems = None, [Msg("No server folder is set up for Stripes on this PC.")]
        tab = hx_ui.MapleJetTab(self.root, services)
        self.assertEqual(self.labels(tab.empty_frame), ["No server folder is set up for Stripes on this PC."])
        tab.destroy()
        # No active lines: the hint is for administrators only
        services = self.make("akim", admin=True, sender=False)
        data = services.data.current
        services.data.current = dataclasses.replace(data, lines={
            k: dataclasses.replace(v, active=False) for k, v in data.lines.items()})
        tab = hx_ui.MapleJetTab(self.root, services)
        self.assertEqual(self.labels(tab.empty_frame),
                         ["No lines are set up yet.", "Open Admin... to add printers, lines and machines."])
        tab.destroy()
        # The administrators' strip: problems, then alerts for "admins"
        services = self.make("akim", admin=True, sender=False)
        services.data.problems = [Msg("Update Stripes")]
        tab = hx_ui.MapleJetTab(self.root, services)
        foreign = alert("al_f", to=("admins", "monitor"), sound=False, colour="", text="Something else is talking to "
                                                                                       "printer {machine}")
        services.bus.post(BoardUpdate(board=board(line("ln_1", "Line 1", [view("mc_1", alerts=[foreign])]))))
        services.bus.pump()
        self.assertEqual([lb.cget("text") for lb in tab.admin_labels],
                         ["Update Stripes", "Line 1 Bagger 1: Something else is talking to printer mc_a"])
        self.assertTrue(shown(tab.admin_frame))
        tab.destroy()
        # ... which a sender doesn't see
        services = self.make("jsmith", admin=False)
        tab = hx_ui.MapleJetTab(self.root, services)
        services.bus.post(BoardUpdate(board=board(line("ln_1", "Line 1", [view("mc_1", alerts=[foreign])]))))
        services.bus.pump()
        self.assertEqual(tab.admin_labels, [])
        self.assertFalse(shown(tab.admin_frame))

    def test_tab_shows_problems_that_come_with_the_boards(self):
        # DataService posts no DataUpdate while current is None, but the watcher's boards keep coming
        services = self.make(load=False)
        tab = hx_ui.MapleJetTab(self.root, services)
        self.assertEqual(self.labels(tab.empty_frame), ["Loading the MapleJet settings..."])
        services.data.problems = [Msg("Update Stripes")]
        services.bus.post(BoardUpdate(board=BoardView(lines=[], share_ok=True, generated_at=0.0)))
        services.bus.pump()
        self.assertEqual(self.labels(tab.empty_frame), ["Update Stripes"])
        self.assertFalse(shown(tab.board))

    def test_tab_banner_and_footer(self):
        services = self.make()
        services.clock = FakeClock()
        tab = hx_ui.MapleJetTab(self.root, services)
        services.bus.post(BoardUpdate(board=board(standalone=True, share_ok=False, next_check_s=18.0)))
        services.bus.post(PollerUpdate(mode="standalone", polling=["pr_1", "pr_2"], next_check_s=18.0))
        services.bus.pump()
        self.assertTrue(shown(tab.banner_frame) and shown(tab.refresh_button))
        self.assertEqual(tab.banner_label.cget("text"),
                         "Server unreachable: showing this PC's own checks · next check in 18 s")
        self.assertEqual(tab.footer_label.cget("text"),
                         "This PC is checking 2 printers on its own while the server can't be reached.")
        services.clock.advance(5.0)
        tab._show_banner()
        self.assertIn("next check in 13 s", tab.banner_label.cget("text"))
        # Refresh now, refused within refresh_now_min_s
        with mock.patch.object(services.poller, "refresh_now", return_value=False):
            tab.refresh_button.invoke()
        self.assertEqual(tab.banner_label.cget("text"),
                         f"You can refresh once every {int(SCALED.refresh_now_min_s)} s.")
        # The timer's end without waiting for it; cancelled, so it doesn't fire after this class's root is gone
        tab.after_cancel(tab._refused_timer)
        tab._end_refused()
        self.assertIn("next check in", tab.banner_label.cget("text"))
        # Back to normal: no banner
        services.bus.post(BoardUpdate(board=board()))
        services.bus.pump()
        self.assertFalse(shown(tab.banner_frame))

    def test_tab_buttons_and_shown(self):
        services = self.make()
        tab = hx_ui.MapleJetTab(self.root, services)
        nav = services.nav
        with mock.patch.object(nav, "set_clocks") as set_clocks, mock.patch.object(nav, "open_send") as open_send:
            tab.clocks_button.invoke()
            tab.send_button.invoke()
        set_clocks.assert_called_once_with([self.world.ids["line"]])
        open_send.assert_called_once_with()
        tab.settings_button.invoke()
        dialog = nav.window("settings")
        self.assertIsInstance(dialog, UserSettingsDialog)
        tab.settings_button.invoke()
        self.assertIs(nav.window("settings"), dialog)              # one per key
        with mock.patch.object(services.poller, "set_monitor_open") as flag:
            tab.on_shown()
            self.assertTrue(nav.tab_shown)
            tab.on_hidden()
            self.assertFalse(nav.tab_shown)
        # The main window is withdrawn here, so the shown tab doesn't count as an open monitor
        self.assertEqual(flag.call_args_list, [mock.call(False, floor_board=False)])

    def test_tab_counts_as_a_monitor_while_viewable(self):
        # ui.md §15.3 rule 2, §4.7, decision 25: the tab shown in a visible main window is an open
        # monitor; hidden to the tray (seen on the next BoardUpdate) it isn't
        services = self.make()
        nav = services.nav
        tab = hx_ui.MapleJetTab(self.root, services)
        tab.pack()
        self.root.deiconify()
        self.addCleanup(self.root.withdraw)
        self.root.update()
        self.assertTrue(self.root.winfo_viewable())
        with mock.patch.object(services.poller, "set_monitor_open") as flag:
            tab.on_shown()
            self.assertEqual(flag.call_args_list, [mock.call(True, floor_board=False)])
            self.root.withdraw()                            # hidden to the tray
            self.root.update()
            nav.update_monitor_flag()                       # what each BoardUpdate does
            self.root.deiconify()
            self.root.update()
            nav.update_monitor_flag()
            tab.on_hidden()
        self.assertEqual(flag.call_args_list, [mock.call(True, floor_board=False), mock.call(False, floor_board=False),
                                               mock.call(True, floor_board=False), mock.call(False, floor_board=False)])


class LineBoardTests(UiCase):
    def board(self):
        a = view("mc_1", origin="Product of USA and Canada", can_stop=True, confirm_pending_by="example\\JSMITH",
                 confirm_send_id="sd_1", details=[Msg("Message {name} in slot {slot}.", {"name": "EXCO1", "slot": 9})])
        failed = alert("al_1", machine="mc_2")
        b = view("mc_2", colour=RED, code="failed_before_switch", headline="Send failed", origin="", needs_send=True,
                 can_stop=True, alerts=[failed], details=[failed.msg, Msg("Confirmed by {user}.", {"user": "x"})])
        c = view("mc_3", colour=BLUE, code="send_in_progress", headline="Being sent", can_stop=False)
        off = view("mc_4", line="ln_2", colour=GREY, code="off", headline="Printer off", printing=False,
                   name="Line 2 Bagger 4", can_resume=True)
        return board(line("ln_1", "Line 1", [a, b, c]), line("ln_2", "Line 2", [off]))

    def test_line_board(self):
        services = self.make()
        services.clock = FakeClock()
        lb = LineBoard(self.root, services)
        self.assertEqual(self.labels(lb.scroller.inner), ["Checking..."])
        lb.show(self.board())
        self.assertEqual(list(lb.cards), ["ln_1", "ln_2"])
        card = lb.cards["ln_1"]
        self.assertEqual(list(card.rows), ["mc_1", "mc_2", "mc_3"])
        self.assertEqual(card.name_label.cget("text"), "Line 1")
        rows = card.rows
        # Origins are bag text; "" shows "(origin unknown)" next to an empty BagText
        self.assertIsInstance(rows["mc_1"].origin, hx_ui_common.BagText)
        self.assertEqual(rows["mc_1"].origin.get(), "Product of USA and Canada")
        self.assertEqual(rows["mc_1"].origin.itemcget(rows["mc_1"].origin.text_item, "text"),
                         "Product of USA and Canada")
        self.assertFalse(shown(rows["mc_1"].unknown_label))
        self.assertEqual(rows["mc_2"].origin.get(), "")
        self.assertTrue(shown(rows["mc_2"].unknown_label))
        self.assertEqual(rows["mc_2"].unknown_label.cget("text"), "(origin unknown)")
        # Buttons follow the flags and the roles; the confirm button only for the sender
        r1, r2, r3 = rows["mc_1"], rows["mc_2"], rows["mc_3"]
        self.assertEqual([shown(b) for b in (r1.stop_button, r1.resume_button, r1.send_button, r1.confirm_button,
                                             r1.ack_button, r1.setup_button)], [True, False, False, True, False, False])
        self.assertEqual([shown(b) for b in (r2.stop_button, r2.send_button, r2.confirm_button, r2.ack_button)],
                         [True, True, False, True])
        self.assertTrue(shown(lb.cards["ln_2"].rows["mc_4"].resume_button))
        self.assertFalse(shown(r3.buttons))                             # BLUE: no buttons at all
        self.assertFalse(any(shown(b) for b in r3._buttons))
        # Details: one per line, without the alert's own text; alerts with their acknowledgement
        self.assertEqual(r1.details_label.cget("text"), "Message EXCO1 in slot 9.")
        self.assertEqual(r2.details_label.cget("text"), "Confirmed by x.")
        self.assertEqual([lb_.cget("text") for lb_ in r2.alert_labels], ["♪ Send failed on mc_2 · not acknowledged"])
        # Line buttons: shown by role, enabled by state; Send is the main button when a machine needs one
        self.assertEqual(card.send_button.cget("style"), "Hx.Main.TButton")
        self.assertEqual(lb.cards["ln_2"].send_button.cget("style"), "TButton")
        self.assertFalse(card.stop_button.instate(["disabled"]))
        self.assertTrue(card.resume_button.instate(["disabled"]))
        self.assertFalse(card.clocks_button.instate(["disabled"]))
        self.assertFalse(card.ack_button.instate(["disabled"]))
        self.assertTrue(lb.cards["ln_2"].ack_button.instate(["disabled"]))
        # Ages count up between updates
        self.assertEqual(r1.age_label.cget("text"), "checked 2 s ago")
        services.clock.advance(3.0)
        lb.update_ages()
        self.assertEqual(r1.age_label.cget("text"), "checked 5 s ago")
        self.assertEqual(card.age_label.cget("text"), "checked 5 s ago")
        # The buttons do what the table says
        nav, watcher = services.nav, services.watcher
        with mock.patch.object(nav, "stop") as stop, mock.patch.object(nav, "open_result") as open_result, \
                mock.patch.object(nav, "open_send") as open_send, mock.patch.object(watcher, "acknowledge") as ack, \
                mock.patch.object(nav, "resume") as resume, mock.patch.object(nav, "set_clocks") as set_clocks:
            r1.stop_button.invoke()
            r1.confirm_button.invoke()
            r2.send_button.invoke()
            r2.ack_button.invoke()
            card.stop_button.invoke()
            card.ack_button.invoke()
            card.clocks_button.invoke()
            lb.cards["ln_2"].resume_button.invoke()
        self.assertEqual(stop.call_args_list, [mock.call(["mc_1"]), mock.call(["mc_1", "mc_2"])])
        open_result.assert_called_once_with("sd_1")
        open_send.assert_called_once_with(line_ids=["ln_1"])
        self.assertEqual(ack.call_args_list, [mock.call(["al_1"]), mock.call(["al_1"])])
        set_clocks.assert_called_once_with(["ln_1"])
        resume.assert_called_once_with(["mc_4"])

    def test_line_board_updates_in_place_and_by_role(self):
        services = self.make()
        lb = LineBoard(self.root, services)
        lb.show(self.board())
        card = lb.cards["ln_1"]
        changed = self.board()
        changed.lines[0].machines[0] = dataclasses.replace(changed.lines[0].machines[0], origin_line="Product of USA")
        services.bus.post(BoardUpdate(board=changed))
        services.bus.pump()
        self.assertIs(lb.cards["ln_1"], card)                          # same machines: updated in place
        self.assertEqual(card.rows["mc_1"].origin.get(), "Product of USA")
        more = self.board()
        more.lines[1].machines.append(view("mc_5", line="ln_2", name="Line 2 Bagger 5"))
        lb.show(more)
        self.assertIsNot(lb.cards["ln_1"], card)                       # different machines: rebuilt
        self.assertEqual(list(lb.cards["ln_2"].rows), ["mc_4", "mc_5"])
        lb.destroy()
        # Someone else's Send can't be confirmed here
        services = self.make("akim", sender=True, admin=False)
        lb = LineBoard(self.root, services)
        lb.show(self.board())
        self.assertFalse(shown(lb.cards["ln_1"].rows["mc_1"].confirm_button))
        lb.destroy()
        # A plain user watches: no Stop, Send, Acknowledge or line buttons
        services = self.make("akim", sender=False, admin=False)
        lb = LineBoard(self.root, services)
        lb.show(self.board())
        card = lb.cards["ln_1"]
        r2 = card.rows["mc_2"]
        self.assertFalse(any(shown(b) for b in r2._buttons))
        self.assertFalse(shown(card.buttons))
        # An administrator sets up a machine without a printer
        services = self.make("akim", sender=False, admin=True)
        lb = LineBoard(self.root, services)
        lb.show(board(line("ln_1", "Line 1", [view("mc_1", colour=GREY, code="not_set_up", headline="No printer "
                                                   "set up", monitored=False, age=None, origin="")])))
        row = lb.cards["ln_1"].rows["mc_1"]
        self.assertTrue(shown(row.setup_button))
        self.assertEqual(row.age_label.cget("text"), "Not checked yet")
        with mock.patch.object(services.nav, "open_admin") as open_admin:
            row.setup_button.invoke()
        open_admin.assert_called_once_with(page="machines", record_id="mc_1")

    def test_row_closes_up_when_alerts_end(self):
        # An emptied frame left packed keeps the height its last children gave it: when a
        # machine's alerts end, its row must close up, not keep a blank gap
        services = self.make()
        lb = LineBoard(self.root, services)
        quiet = board(line("ln_1", "Line 1", [view("mc_1")]))
        lb.show(quiet)
        self.root.update_idletasks()
        row = lb.cards["ln_1"].rows["mc_1"]
        plain_h = row.winfo_reqheight()
        self.assertFalse(shown(row.alerts_frame))
        self.assertFalse(shown(row.details_label))
        failed = [alert("al_1", machine="mc_1"), alert("al_2", machine="mc_1", text="Ink out on {machine}")]
        lb.show(board(line("ln_1", "Line 1", [view("mc_1", colour=RED, alerts=failed, needs_send=True,
                                                   details=[Msg("Message {name} in slot {slot}.",
                                                                {"name": "EXCO1", "slot": 9})])])))
        self.root.update_idletasks()
        self.assertIs(lb.cards["ln_1"].rows["mc_1"], row)                  # updated in place
        self.assertTrue(shown(row.alerts_frame))
        self.assertGreater(row.winfo_reqheight(), plain_h + row.alerts_frame.winfo_reqheight() - 1)
        # Details, then alerts, then buttons
        body = row.alerts_frame.master
        self.assertEqual([w for w in body.pack_slaves() if w in (row.details_label, row.alerts_frame, row.buttons)],
                         [row.details_label, row.alerts_frame, row.buttons])
        lb.show(quiet)
        self.root.update_idletasks()
        self.assertFalse(shown(row.alerts_frame))
        self.assertEqual(row.alert_labels, [])
        self.assertEqual(row.winfo_reqheight(), plain_h)                    # no gap left behind
        # Alerts again, then only the details go: the alerts stay above the buttons
        lb.show(board(line("ln_1", "Line 1", [view("mc_1", colour=RED, alerts=failed[:1], needs_send=True)])))
        self.assertEqual([w for w in body.pack_slaves() if w in (row.details_label, row.alerts_frame, row.buttons)],
                         [row.alerts_frame, row.buttons])
        lb.destroy()

    def test_not_monitored_and_empty(self):
        services = self.make()
        lb = LineBoard(self.root, services)
        lost = view("mc_1", colour=GREY, code="not_monitored", headline="NOT MONITORED", monitored=False, age=41.0)
        lb.show(board(line("ln_1", "Line 1", [lost], headline="NOT MONITORED")))
        card = lb.cards["ln_1"]
        self.assertEqual(card.name_label.cget("foreground"), DIM_FG)
        self.assertEqual(card.headline_label.cget("foreground"), DIM_FG)
        self.assertEqual(card.rows["mc_1"].name_label.cget("foreground"), DIM_FG)
        self.assertEqual(card.rows["mc_1"].origin.itemcget(card.rows["mc_1"].origin.text_item, "fill"), DIM_FG)
        # One machine still watched: the line keeps its colours
        lb.show(board(line("ln_1", "Line 1", [lost, view("mc_2", colour=RED)], colour=RED)))
        self.assertEqual(lb.cards["ln_1"].name_label.cget("foreground"), hx_ui_common.TEXT_FG)
        # An empty board while the settings have active lines: the next board is on its way
        lb.show(board())
        self.assertEqual(lb.cards, {})
        self.assertEqual(self.labels(lb.scroller.inner), ["Checking..."])
        no_active_lines(services)
        lb.show(board())
        self.assertEqual(self.labels(lb.scroller.inner), ["No lines are set up yet."])

    def test_empty_board_while_the_settings_are_not_loaded(self):
        # What the watcher posts while DataService.current is None (states.md §7 step 1) isn't
        # "No lines are set up yet." (ui.md §4.6, §9.1 rule 4)
        services = self.make(load=False)
        lb = LineBoard(self.root, services)
        services.nav.open_monitor()
        monitor = services.nav.window("monitor")
        services.bus.post(BoardUpdate(board=BoardView(lines=[], share_ok=True, generated_at=0.0)))
        services.bus.pump()
        self.assertEqual(self.labels(lb.scroller.inner), ["Loading the MapleJet settings..."])
        self.assertEqual(self.labels(monitor.board.scroller.inner), ["Loading the MapleJet settings..."])
        services.data.problems = [Msg("Update Stripes"), Msg("No server folder is set up for Stripes on this PC.")]
        services.bus.post(BoardUpdate(board=BoardView(lines=[], share_ok=True, generated_at=0.0)))
        services.bus.pump()
        self.assertEqual(self.labels(lb.scroller.inner),
                         ["Update Stripes\nNo server folder is set up for Stripes on this PC."])
        self.assertEqual(lb.message_label.cget("foreground"), COLOUR_HEX[AMBER])
        i18n.set_language("es")
        lb.retranslate()
        self.assertEqual(self.labels(lb.scroller.inner), ["Update Stripes\nEsta PC no tiene una carpeta del servidor "
                                                          "configurada para Stripes."])
        i18n.set_language("en")
        services.data.problems = []
        services.bus.post(BoardUpdate(board=BoardView(lines=[], share_ok=True, generated_at=0.0)))
        services.bus.pump()
        self.assertEqual(self.labels(lb.scroller.inner), ["Loading the MapleJet settings..."])
        self.assertEqual(lb.message_label.cget("foreground"), hx_ui_common.MUTED_FG)
        lb.destroy()

    def test_strict_threads(self):
        services = self.make()
        bag = hx_ui_common.BagText(self.root)
        lb = LineBoard(self.root, services)
        raised = []

        def worker():
            for call in (lambda: bag.set("Product of USA"), lambda: lb.show(board())):
                try:
                    call()
                except RuntimeError as e:
                    raised.append(str(e))
        t = threading.Thread(target=worker)
        t.start()
        t.join(5)
        self.assertEqual(raised, ["Tk call off the Tk thread"] * 2)


class MonitorTests(UiCase):
    def test_monitor_window(self):
        services = self.make()
        services.nav.open_monitor()
        window = services.nav.window("monitor")
        self.assertIsInstance(window, MonitorWindow)
        self.assertEqual(window.title(), "MapleJet monitor")
        services.bus.post(BoardUpdate(board=board(line("ln_1", "Line 1", [view("mc_1", colour=RED), view("mc_2")]),
                                                  line("ln_2", "Line 2", [view("mc_3", colour=AMBER)]))))
        services.bus.pump()
        summary = window.summary_label.cget("text")
        self.assertTrue(summary.startswith("1 red · 1 amber · 1 green · updated "), summary)
        self.assertEqual(list(window.board.cards), ["ln_1", "ln_2"])
        self.assertFalse(shown(window.banner_label))
        # Refresh now: the watcher in share mode, the poller in standalone mode (refused within 10 s)
        with mock.patch.object(services.watcher, "refresh_now") as refresh:
            window.refresh_button.invoke()
        refresh.assert_called_once_with()
        services.bus.post(BoardUpdate(board=board(standalone=True, share_ok=False, next_check_s=7.0)))
        services.bus.pump()
        self.assertTrue(shown(window.banner_label))
        services.poller.mode = "standalone"
        with mock.patch.object(services.poller, "refresh_now", return_value=False):
            window.refresh_button.invoke()
        self.assertTrue(shown(window.refused_label))
        self.assertEqual(window.refused_label.cget("text"),
                         f"You can refresh once every {int(SCALED.refresh_now_min_s)} s.")
        # One window per key; Floor board opens the floor board
        services.nav.open_monitor()
        self.assertEqual([w for w in hx_ui_common.open_windows() if isinstance(w, MonitorWindow)], [window])
        window.floor_button.invoke()
        self.assertIsInstance(services.nav.window("floor"), FloorBoard)
        # In Spanish
        i18n.set_language("es")
        hx_ui.language_changed(services)
        self.assertEqual(window.title(), "Monitor MapleJet")
        self.assertEqual(window.floor_button.cget("text"), "Tablero de planta")

    def test_monitor_flag(self):
        services = self.make()
        nav = services.nav
        with mock.patch.object(services.poller, "set_monitor_open") as flag:
            nav.open_monitor()
            self.assertEqual(flag.call_args_list, [mock.call(True, floor_board=False)])
            nav.open_floor_board()
            self.assertEqual(flag.call_args_list[-1], mock.call(True, floor_board=True))
            nav.window("monitor").close()
            self.assertEqual(len(flag.call_args_list), 2)          # still open: the floor board
            nav.window("floor").close_button.invoke()
            self.assertEqual(flag.call_args_list, [mock.call(True, floor_board=False), mock.call(True, floor_board=True),
                                                   mock.call(False, floor_board=False)])
            self.assertFalse(nav.floor_board_open())


class FloorBoardTests(UiCase):
    def board(self):
        ringing = alert("al_f", to=("floor", "user:EXAMPLE\\akim"), machine="mc_2")
        a = view("mc_1", origin="Product of USA")
        b = view("mc_2", origin="Product of USA and Canada", colour=RED, code="drift", headline="Drift",
                 alerts=[ringing])
        c = view("mc_3", line="ln_2", colour=AMBER, code="not_monitored", headline="NOT MONITORED", monitored=False,
                 origin="USA (pre-printed)", age=41.0, name="Line 2 Bagger 3")
        d = view("mc_4", line="ln_2", origin="", name="Line 2 Bagger 4")
        return board(line("ln_1", "Line 1", [a, b]), line("ln_2", "Line 2", [c, d]))

    def open(self, services, b=None, **kw):
        with mock.patch.object(hx_win, "monitors", return_value=kw.pop("monitors", [])):
            fb = FloorBoard(self.root, services, **kw)
        fb.show(self.board() if b is None else b)
        self.root.update_idletasks()
        return fb

    @staticmethod
    def px(fb, item):
        """The pixel size an item is drawn at."""
        return int(re.search(r"-(\d+)", str(fb.canvas.itemcget(item, "font"))).group(1))

    @staticmethod
    def texts(canvas, items):
        return {key: canvas.itemcget(item, "text") for key, item in items.items()}

    def test_floor_board(self):
        services = self.make()
        fb = self.open(services, size="1280x800")
        self.assertEqual(fb.geometry_spec, "1280x800+0+0")
        self.assertFalse(fb.full_screen)
        self.assertEqual(fb.title(), "MapleJet floor board")
        self.root.update()
        self.assertTrue(fb.geometry().startswith("1280x800"))
        c = fb.canvas
        self.assertEqual(fb.header.itemcget(fb.title_item, "text"), "MapleJet floor board")
        self.assertEqual([r.machine_id for r in fb.rows], ["mc_1", "mc_2", "mc_3", "mc_4"])
        # Rows grouped under their line's name; each machine shows its name without the line's
        self.assertEqual(self.texts(c, fb.line_items), {"ln_1": "Line 1", "ln_2": "Line 2"})
        self.assertEqual(self.texts(c, fb.name_items),
                         {"mc_1": "Bagger 1", "mc_2": "Bagger 2", "mc_3": "Bagger 3", "mc_4": "Bagger 4"})
        self.assertEqual(self.texts(c, fb.origin_items), {r.machine_id: r.origin_line for r in fb.rows})
        self.assertEqual(c.itemcget(fb.unknown_items["mc_4"], "text"), "(origin unknown)")
        self.assertEqual(list(fb.unknown_items), ["mc_4"])
        self.assertEqual(c.itemcget(fb.status_items["mc_1"], "text"), "Printing")
        self.assertEqual(c.itemcget(fb.status_items["mc_2"], "fill"), COLOUR_HEX[RED])
        self.assertEqual(c.itemcget(fb.age_items["mc_1"], "text"), "checked 2 s ago")
        # NOT MONITORED: grey text, but the bar keeps its last colour; its line has a machine
        # still watched, so the line's name keeps its colour
        for items in (fb.name_items, fb.origin_items, fb.status_items, fb.age_items):
            self.assertEqual(c.itemcget(items["mc_3"], "fill"), DIM_FG)
        self.assertEqual(c.itemcget(fb.bar_items["mc_3"], "fill"), COLOUR_HEX[AMBER])
        self.assertEqual(c.itemcget(fb.line_items["ln_2"], "fill"), hx_ui_common.TEXT_FG)
        # The bar of a machine with an unacknowledged sound alert flashes; the others don't
        self.assertEqual(c.itemcget(fb.bar_items["mc_2"], "fill"), COLOUR_HEX[RED])
        fb.flash()
        self.assertEqual((c.itemcget(fb.bar_items["mc_2"], "fill"), fb.flash_on), (CARD_BG, False))
        self.assertEqual(c.itemcget(fb.bar_items["mc_1"], "fill"), COLOUR_HEX[GREEN])
        fb.redraw()                                                     # a redraw keeps the beat
        self.assertEqual(c.itemcget(fb.bar_items["mc_2"], "fill"), CARD_BG)
        fb.flash()
        self.assertEqual(c.itemcget(fb.bar_items["mc_2"], "fill"), COLOUR_HEX[RED])
        # Layout from the window's size: 4 machines under 2 line headers
        self.assertEqual(fb.layout, floor_layout(1280, 800, 4, 2))
        # Acknowledge: shown to someone who may, while a floor alert rings; Refresh only standalone
        self.assertTrue(shown(fb.ack_button) and shown(fb.close_button))
        self.assertFalse(shown(fb.refresh_button))
        with mock.patch.object(services.watcher, "acknowledge") as ack:
            fb.ack_button.invoke()
        ack.assert_called_once_with(["al_f"])
        # A line nobody checks: its name greyed out too
        lost = view("mc_5", line="ln_3", colour=GREY, code="not_monitored", headline="NOT MONITORED",
                    monitored=False, name="Line 3 Bagger 5")
        fb.show(board(*self.board().lines, line("ln_3", "Line 3", [lost])))
        self.assertEqual(c.itemcget(fb.line_items["ln_3"], "fill"), DIM_FG)
        fb.destroy()
        plain = self.make("akim", sender=False, admin=False)
        fb = self.open(plain)
        self.assertFalse(shown(fb.ack_button))
        self.assertTrue(shown(fb.close_button))

    def test_printed_code_when_the_name_is_the_line_name(self):
        services = self.make()
        data = services.data.current
        (mid, machine), = data.machines.items()
        services.data.current = dataclasses.replace(data, machines={mid: dataclasses.replace(machine, name="Line 1")})
        b = board(line(machine.line_id, "Line 1", [view(mid, line=machine.line_id, name="Line 1"),
                                                   view("mc_x", line=machine.line_id, name="Line 1")]))
        fb = self.open(services, b, size="1280x800")
        self.assertEqual(self.texts(fb.canvas, fb.name_items), {mid: "B1", "mc_x": "Line 1"})   # no record: the name

    def test_screen_and_size(self):
        services = self.make()
        fb = self.open(services, monitors=[])                     # no list: Tk's screen, full screen
        self.assertTrue(fb.full_screen)
        self.assertEqual(fb.geometry_spec,
                         f"{self.root.winfo_screenwidth()}x{self.root.winfo_screenheight()}+0+0")
        fb.destroy()
        mons = [hx_win.Monitor(0, 0, 0, 1920, 1080, True), hx_win.Monitor(1, 1920, 0, 1280, 1024, False)]
        fb = self.open(services, monitors=mons, screen=1)
        self.assertEqual(fb.geometry_spec, "1280x1024+1920+0")
        fb.destroy()
        fb = self.open(services, monitors=mons, screen=5, size="2560x1440")   # a missing screen: the main one
        self.assertEqual(fb.geometry_spec, "1920x1080+0+0")                   # no bigger than the screen
        fb.destroy()
        fb = self.open(services, monitors=mons, size="huge")
        self.assertTrue(fb.full_screen)

    def check_rows(self, fb, size):
        """Every row inside the room under the header, every text inside its column and its row,
        and one size per column; returns the rows' (y0, y1)."""
        c, lay = fb.canvas, fb.layout
        width, height = (int(n) for n in size.split("x"))
        self.assertEqual(fb.layout, floor_layout(width, height, len(fb.rows), len(fb.line_items),
                                                 fb.scrollbar.winfo_reqwidth()))
        columns = {"name": (lay.bar_w, lay.bar_w + lay.names_w)}
        columns["origin"] = (columns["name"][1], columns["name"][1] + lay.origin_w)
        columns["status"] = (columns["origin"][1], columns["origin"][1] + lay.status_w)
        columns["age"] = (columns["status"][1], lay.width)
        spans = []
        for r in fb.rows:
            mid = r.machine_id
            x0, y0, x1, y1 = (round(float(v)) for v in c.coords(fb.bar_items[mid]))
            self.assertEqual((x0, x1), (0, lay.bar_w))
            spans.append((y0, y1))
            items = {"name": fb.name_items[mid], "origin": fb.origin_items[mid], "status": fb.status_items[mid],
                     "age": fb.age_items[mid]}
            if mid in fb.unknown_items:
                items["unknown"] = fb.unknown_items[mid]
            for column, item in items.items():
                if not c.itemcget(item, "text"):
                    continue
                left, right = columns["origin" if column == "unknown" else column]
                bx0, by0, bx1, by1 = c.bbox(item)
                with self.subTest(size=size, machine=mid, column=column):
                    self.assertGreaterEqual(bx0, left)
                    self.assertLessEqual(bx1, right)            # no overlap with the next column
                    self.assertGreaterEqual(by0, y0)
                    self.assertLessEqual(by1, y1)               # nothing wraps onto two lines
                    self.assertEqual(float(c.itemcget(item, "width")), 0.0)
        # One size per column on every row
        for column, items in (("name", fb.name_items), ("origin", fb.origin_items), ("status", fb.status_items),
                              ("age", fb.age_items)):
            self.assertEqual({self.px(fb, i) for i in items.values()}, {fb.font_px[column]}, column)
        # Rows don't overlap, and line headers sit between the lines
        for (_, a1), (b0, _) in zip(spans, spans[1:]):
            self.assertLessEqual(a1, b0)
        return spans

    def big_board(self, lines, per_line):
        out, k = [], 0
        for li in range(1, lines + 1):
            machines = []
            for b in range(1, per_line + 1):
                k += 1
                colour = (GREEN, AMBER, RED, GREY)[k % 4]
                machines.append(view(f"mc_{k:02d}", line=f"ln_{li}", name=f"Line {li} Bagger {b}", colour=colour,
                                     headline=("Printing", "Send not confirmed", "TEST by EXAMPLE\\akim, step 12 of 40",
                                               "Printer off")[k % 4],
                                     origin=("Product of USA, Canada, and Mexico", "USA (pre-printed)",
                                             "Product of Canada", "")[k % 4]))
            out.append(line(f"ln_{li}", f"Line {li}", machines))
        return board(*out)

    def test_rows_fit_without_overlap(self):
        # The screenshots' boards at the two screen sizes the floor PCs use: every row on screen,
        # nothing wrapped, nothing running into the next column, one origin size
        services = self.make()
        screens = [hx_win.Monitor(0, 0, 0, 1920, 1080, True)]
        six = board(line("ln_1", "Line 1", [view(f"mc_{i}", origin="Product of USA and Canada") for i in (1, 2, 3)]),
                    line("ln_2", "Line 2", [
                        view("mc_4", line="ln_2", name="Line 2 Bagger 1", colour=RED, code="left_red",
                             headline="Left red by EXAMPLE\\jsmith", origin="Product of Mexico"),
                        view("mc_5", line="ln_2", name="Line 2 Bagger 2", colour=AMBER, code="unconfirmed",
                             headline="Send not confirmed", origin="Product of Mexico and USA"),
                        view("mc_6", line="ln_2", name="Line 2 Bagger 3", colour=GREY, code="off",
                             headline="Printer off", origin="", printing=False)]))
        family = hx_ui_common.bag_font_family(self.root)
        for size in ("1280x800", "1920x1080"):
            for b in (six, self.big_board(4, 3)):
                fb = self.open(services, b, size=size, monitors=screens)
                self.root.update()
                lay = fb.layout
                self.assertFalse(lay.scrolls)
                self.assertFalse(shown(fb.scrollbar))
                spans = self.check_rows(fb, size)
                self.assertLessEqual(spans[-1][1], fb.canvas.winfo_height())     # the last row isn't cut off
                self.assertEqual(fb.canvas.winfo_height(), int(size.split("x")[1]) - lay.header_h)
                # The origin size is the largest at which the widest origin fits its column
                origins = [r.origin_line for r in fb.rows if r.origin_line]
                room = lay.origin_w - 24
                size_px = fb.font_px["origin"]
                widest = max(fb._font(family, size_px, "bold").measure(o) for o in origins)
                self.assertLessEqual(widest, room)
                if size_px < lay.origin_px:
                    self.assertGreater(max(fb._font(family, size_px + 1, "bold").measure(o) for o in origins), room)
                self.assertGreaterEqual(size_px, 20)                              # and it reads from the floor
                fb.destroy()

    def test_one_origin_size_never_cut_short(self):
        services = self.make()
        long_origin = "Product of " + ", ".join(["USA", "Canada", "Mexico"] * 12) + ", and Peru"
        b = board(line("ln_1", "Line 1", [view("mc_1", origin="Product of USA"), view("mc_2", origin=long_origin)]))
        fb = self.open(services, b, size="1280x800")
        c, lay = fb.canvas, fb.layout
        family = hx_ui_common.bag_font_family(fb)
        # Both origins at one size: the long one shrinks the short one too
        self.assertEqual(self.px(fb, fb.origin_items["mc_1"]), self.px(fb, fb.origin_items["mc_2"]))
        px = fb.font_px["origin"]
        self.assertLess(px, lay.origin_px)
        self.assertLessEqual(fb._font(family, px, "bold").measure(long_origin), lay.origin_w - 24)
        self.assertGreater(fb._font(family, px + 1, "bold").measure(long_origin), lay.origin_w - 24)
        # Never cut short, never wrapped
        self.assertEqual(self.texts(c, fb.origin_items), {"mc_1": "Product of USA", "mc_2": long_origin})
        self.check_rows(fb, "1280x800")
        # Without the long origin the board's origins grow again
        fb.show(board(line("ln_1", "Line 1", [view("mc_1", origin="Product of USA")])))
        self.assertGreater(fb.font_px["origin"], px)

    def test_long_names_and_statuses_get_an_ellipsis(self):
        services = self.make()
        status = "Send interrupted on FLOOR01: check the bags now, then send again from FLOOR02"
        b = board(line("ln_1", "Line 1", [
            view("mc_1", name="Line 1 Bagger 1 by the north loading dock door", headline=status,
                 origin="Product of USA, Canada, and Mexico"),
            view("mc_2")]))
        fb = self.open(services, b, size="1280x800")
        c = fb.canvas
        name, shown_status = c.itemcget(fb.name_items["mc_1"], "text"), c.itemcget(fb.status_items["mc_1"], "text")
        self.assertTrue(name.startswith("Bagger 1 ") and name.endswith("…"), name)
        self.assertTrue(status.startswith(shown_status[:-1]) and shown_status.endswith("…"), shown_status)
        self.assertEqual(c.itemcget(fb.status_items["mc_2"], "text"), "Printing")
        self.assertEqual(c.itemcget(fb.origin_items["mc_1"], "text"), "Product of USA, Canada, and Mexico")
        # They shrank as far as allowed first, the same on every row
        self.assertEqual(fb.font_px["status"], min(16, fb.layout.status_px))
        self.assertEqual(fb.font_px["name"], min(14, fb.layout.machine_px))
        self.check_rows(fb, "1280x800")

    def test_scrolls_when_the_rows_cannot_fit(self):
        services = self.make()
        fb = self.open(services, self.big_board(5, 6), size="1280x800")
        self.root.update()
        c, lay = fb.canvas, fb.layout
        self.assertTrue(lay.scrolls)
        self.assertTrue(shown(fb.scrollbar))
        self.assertEqual(lay.row_h, hx_ui_monitor.FLOOR_MIN_ROW_PX)
        self.assertEqual(c.winfo_width(), 1280 - fb.scrollbar.winfo_reqwidth())
        self.assertEqual([float(v) for v in str(c.cget("scrollregion")).split()], [0, 0, lay.width, lay.rows_h])
        spans = self.check_rows(fb, "1280x800")
        self.assertGreater(spans[-1][1], c.winfo_height())                   # the last rows are below ...
        self.assertEqual(c.yview()[0], 0.0)
        fb._on_wheel(mock.Mock(num=5, delta=0))                              # ... and the wheel brings them up
        fb._on_wheel(mock.Mock(num=0, delta=-240))
        top = c.yview()[0]
        self.assertGreater(top, 0.0)
        fb.show(self.big_board(5, 6))                                        # a new board keeps the place
        self.assertEqual(c.yview()[0], top)
        c.yview_moveto(1.0)
        self.root.update()
        self.assertGreaterEqual(round(c.canvasy(c.winfo_height())), lay.rows_h)    # down to the last row
        # Back to a board that fits: no scrollbar, from the top, the wheel does nothing
        fb.show(self.board())
        self.root.update()
        self.assertFalse(fb.layout.scrolls)
        self.assertFalse(shown(fb.scrollbar))
        self.assertEqual(c.yview()[0], 0.0)
        fb._on_wheel(mock.Mock(num=5, delta=0))
        self.assertEqual(c.yview()[0], 0.0)

    def test_ages_tick_and_empty_board(self):
        services = self.make()
        services.clock = FakeClock()
        fb = self.open(services, size="1280x800")
        services.clock.advance(4.0)
        fb.tick()
        self.assertEqual(fb.canvas.itemcget(fb.age_items["mc_1"], "text"), "checked 6 s ago")
        # An age that outgrows its column before the next redraw is shortened, never run into the status
        services.clock.advance(10 ** 9)
        fb.tick()
        age = fb.canvas.itemcget(fb.age_items["mc_1"], "text")
        self.assertTrue(age.endswith("…") and age.startswith("checked"), age)
        lay = fb.layout
        self.assertGreaterEqual(fb.canvas.bbox(fb.age_items["mc_1"])[0],
                                lay.bar_w + lay.names_w + lay.origin_w + lay.status_w)
        # An empty board: the next one is on its way while the settings have active lines
        fb.show(board())
        self.assertEqual(fb.canvas.itemcget(fb.message_item, "text"), "Checking...")
        no_active_lines(services)
        fb.show(board())
        texts = [fb.canvas.itemcget(i, "text") for i in fb.canvas.find_all() if fb.canvas.type(i) == "text"]
        self.assertIn("No lines are set up yet.", texts)
        # Standalone: Refresh now, refused within refresh_now_min_s
        fb.show(board(standalone=True, share_ok=False, next_check_s=9.0))
        self.assertTrue(shown(fb.refresh_button))
        self.assertEqual(fb.header.itemcget(fb.banner_item, "text"),
                         "Server unreachable: showing this PC's own checks · next check in 9 s")
        with mock.patch.object(services.poller, "refresh_now", return_value=False):
            fb.refresh_button.invoke()
        self.assertEqual(fb.header.itemcget(fb.banner_item, "text"),
                         f"You can refresh once every {int(SCALED.refresh_now_min_s)} s.")
        fb.destroy()
        # The banner stops short of the buttons
        fb = self.open(services, board(standalone=True, share_ok=False, next_check_s=9.0), size="800x600")
        self.root.update()
        h = fb.header
        banner = h.itemcget(fb.banner_item, "text")
        self.assertTrue(banner.endswith("…"), banner)
        self.assertLess(h.bbox(fb.banner_item)[2], fb.refresh_button.winfo_x())

    def test_floor_board_while_the_settings_are_not_loaded(self):
        # The watcher's board while DataService.current is None (states.md §7 step 1): the
        # settings' problems or that they are loading, never "No lines are set up yet."
        services = self.make(load=False)
        with mock.patch.object(hx_win, "monitors", return_value=[]):
            fb = FloorBoard(self.root, services, size="800x600")
        self.assertEqual(fb.canvas.itemcget(fb.message_item, "text"), "Checking...")     # no board yet
        services.bus.post(BoardUpdate(board=BoardView(lines=[], share_ok=True, generated_at=0.0)))
        services.bus.pump()
        self.assertEqual(fb.canvas.itemcget(fb.message_item, "text"), "Loading the MapleJet settings...")
        self.assertEqual(fb.canvas.itemcget(fb.message_item, "fill"), hx_ui_common.MUTED_FG)
        services.data.problems = [Msg("Update Stripes")]
        services.bus.post(BoardUpdate(board=BoardView(lines=[], share_ok=True, generated_at=0.0)))
        services.bus.pump()
        texts = [fb.canvas.itemcget(i, "text") for i in fb.canvas.find_all() if fb.canvas.type(i) == "text"]
        self.assertNotIn("No lines are set up yet.", texts)
        self.assertEqual(fb.canvas.itemcget(fb.message_item, "text"), "Update Stripes")
        self.assertEqual(fb.canvas.itemcget(fb.message_item, "fill"), COLOUR_HEX[AMBER])
        i18n.set_language("es")
        services.data.problems = []
        fb.retranslate()
        self.assertEqual(fb.canvas.itemcget(fb.message_item, "text"), "Cargando la configuración de MapleJet...")
        # Once the settings load (DataUpdate), the rows come with the watcher's next board
        i18n.set_language("en")
        services.data.run_once()
        services.bus.pump()
        self.assertEqual(fb.canvas.itemcget(fb.message_item, "text"), "Checking...")
        fb.show(self.board())
        self.assertIsNone(fb.message_item)
        self.assertEqual(len(fb.rows), 4)


class AlertSounderTests(UiCase):
    def setUp(self):
        super().setUp()
        self.play = self.patch(hx_win, "play_alert")
        self.stop = self.patch(hx_win, "stop_alert")

    def test_alert_sounder(self):
        services = self.make()
        nav = services.nav
        sounder = hx_ui_monitor.AlertSounder(services)
        nav.sounder = sounder
        mine = alert("al_1", to=("user:example\\JSMITH", "floor"))
        ringing = board(line("ln_1", "Line 1", [view("mc_1", colour=RED, alerts=[mine])]))
        services.bus.post(BoardUpdate(board=ringing))
        services.bus.pump()
        self.play.assert_called_once_with(repeat=True)
        self.assertTrue(sounder.playing)
        popup = nav.window("alerts")
        self.assertIsInstance(popup, hx_ui_monitor.AlertPopup)
        self.assertEqual([a.id for a in popup.alerts], ["al_1"])
        self.assertEqual(popup.alert_labels[0].cget("text"), "♪ Send failed on mc_a")
        self.assertTrue(shown(popup.ack_button))
        sounder.evaluate(ringing)                                   # a second board doesn't replay
        self.assertEqual(self.play.call_count, 1)
        with mock.patch.object(services.watcher, "acknowledge") as ack:
            popup.ack_button.invoke()
        ack.assert_called_once_with(["al_1"])
        # Acknowledged: the sound stops and the pop-up closes itself
        acked = board(line("ln_1", "Line 1", [view("mc_1", colour=RED, alerts=[
            dataclasses.replace(mine, acked_by="EXAMPLE\\akim")])]))
        services.bus.post(BoardUpdate(board=acked))
        services.bus.pump()
        self.stop.assert_called_once_with()
        self.assertFalse(sounder.playing)
        self.assertIsNone(nav.window("alerts"))
        # A floor alert rings only while the floor board is open, and has no pop-up
        floor = board(line("ln_1", "Line 1", [view("mc_1", colour=RED, alerts=[alert("al_2", to=("floor",))])]))
        services.watcher.latest = floor
        sounder.evaluate(floor)
        self.assertFalse(sounder.playing)
        with mock.patch.object(hx_win, "monitors", return_value=[]):
            nav.open_floor_board()
        self.assertTrue(sounder.playing)
        self.assertEqual(self.play.call_count, 2)
        self.assertIsNone(nav.window("alerts"))
        nav.window("floor").close_button.invoke()
        self.assertFalse(sounder.playing)
        self.assertEqual(self.stop.call_count, 2)
        # Someone else's alert: no sound and no pop-up here
        sounder.evaluate(board(line("ln_1", "Line 1", [view("mc_1", alerts=[
            alert("al_3", to=("user:EXAMPLE\\akim",))])])))
        self.assertFalse(sounder.playing)
        self.assertIsNone(nav.window("alerts"))

    def test_silent_after_stop_services(self):
        # Quitting: stop_services silences first, then App.destroy() destroys the floor board,
        # whose close re-evaluates the sound (ui.md §15.4 item 2, §10.1). Nothing may ring again.
        services = self.make()
        nav = services.nav
        nav.sounder = hx_ui_monitor.AlertSounder(services)
        services.started = True                     # as start_services leaves it
        mine = alert("al_1", to=("user:EXAMPLE\\jsmith",))
        services.watcher.latest = board(line("ln_1", "Line 1", [view("mc_1", colour=RED, alerts=[mine])]))
        with mock.patch.object(hx_win, "monitors", return_value=[]):
            nav.open_floor_board()
        self.assertEqual(self.play.call_count, 1)
        self.assertIsNotNone(nav.window("alerts"))
        hx_ui.stop_services(services, timeout_s=0.2)
        self.assertFalse(nav.sounder.playing)
        self.assertIsNone(nav.window("alerts"))
        nav.window("floor").destroy()
        nav.sounder.evaluate(services.watcher.latest)
        services.bus.post(BoardUpdate(board=services.watcher.latest))
        services.bus.pump()
        self.assertEqual(self.play.call_count, 1)
        self.assertIsNone(nav.window("alerts"))

    def test_popup_comes_back_only_for_a_new_alert(self):
        services = self.make("akim", sender=False, admin=False)
        nav = services.nav
        sounder = hx_ui_monitor.AlertSounder(services)
        nav.sounder = sounder
        first = alert("al_1", to=("user:EXAMPLE\\akim",))
        sounder.evaluate(board(line("ln_1", "Line 1", [view("mc_1", alerts=[first])])))
        popup = nav.window("alerts")
        self.assertFalse(shown(popup.ack_button))                     # can't acknowledge: says who can
        self.assertTrue(shown(popup.no_role_label))
        popup.close_button.invoke()
        sounder.evaluate(board(line("ln_1", "Line 1", [view("mc_1", alerts=[first])])))
        self.assertIsNone(nav.window("alerts"))                        # closed: the sound goes on
        self.assertTrue(sounder.playing)
        second = alert("al_2", to=("user:EXAMPLE\\akim",), started_at=1_793_275_300.0)
        sounder.evaluate(board(line("ln_1", "Line 1", [view("mc_1", alerts=[first, second])])))
        self.assertEqual([a.id for a in nav.window("alerts").alerts], ["al_1", "al_2"])
        sounder.stop()
        self.assertFalse(sounder.playing)
        self.assertIsNone(nav.window("alerts"))
        services.bus.post(BoardUpdate(board=board(line("ln_1", "Line 1", [view("mc_1", alerts=[first])]))))
        services.bus.pump()                                            # unsubscribed
        self.assertFalse(sounder.playing)


class SettingsTests(UiCase):
    def test_user_settings(self):
        services = self.make()
        local = services.local
        self.assertEqual(hx_ui_common.HxSettings.load(local), hx_ui_common.HxSettings())
        for bad in ({"floor_board_at_startup": "yes", "floor_board_screen": -1, "floor_board_size": "huge"},
                    {"floor_board_size": "10x10", "floor_board_screen": 1.5}):
            local.write_user_json("maplejet.json", bad)
            self.assertEqual(hx_ui_common.HxSettings.load(local), hx_ui_common.HxSettings())
        saved = hx_ui_common.HxSettings(floor_board_at_startup=True, floor_board_screen=1, floor_board_size="1280x720")
        saved.save(local)
        self.assertEqual(hx_ui_common.HxSettings.load(local), saved)

    def test_settings_dialog(self):
        services = self.make()
        with mock.patch.object(hx_win, "monitors", return_value=[]):
            dialog = UserSettingsDialog(self.root, services)
        self.assertEqual(dialog.title(), "My MapleJet settings")
        self.assertEqual(dialog.screen_box.cget("values"), ("This screen",))
        self.assertEqual((dialog.screen_box.key(), dialog.size_var.get()), ("0", "full"))
        self.assertEqual(str(dialog.width_spin.cget("state")), "disabled")
        dialog.at_startup_var.set(True)
        dialog.size_var.set("window")
        dialog.window_radio.invoke()
        self.assertEqual(str(dialog.width_spin.cget("state")), "normal")
        dialog.width_var.set("99999")                                   # kept inside the Spinbox's limits
        dialog.height_var.set("720")
        dialog.save_button.invoke()
        self.assertFalse(dialog.winfo_exists())
        want = hx_ui_common.HxSettings(floor_board_at_startup=True, floor_board_screen=0, floor_board_size="7680x720")
        self.assertEqual(services.settings, want)
        self.assertEqual(hx_ui_common.HxSettings.load(services.local), want)
        # Two screens; the saved screen 1 shows; Escape closes without saving
        mons = [hx_win.Monitor(0, 0, 0, 1920, 1080, True), hx_win.Monitor(1, 1920, 0, 1280, 1024, False)]
        services.settings.floor_board_screen = 1
        with mock.patch.object(hx_win, "monitors", return_value=mons):
            dialog = UserSettingsDialog(self.root, services)
        self.assertEqual(dialog.screen_box.cget("values"), ("1: 1920 x 1080 (main)", "2: 1280 x 1024"))
        self.assertEqual(dialog.screen_box.key(), "1")
        i18n.set_language("es")
        dialog.retranslate()
        self.assertEqual(dialog.screen_box.cget("values"), ("1: 1920 x 1080 (principal)", "2: 1280 x 1024"))
        self.assertEqual(dialog.screen_box.key(), "1")
        i18n.set_language("en")
        dialog.at_startup_var.set(False)
        dialog.focus_force()                     # Tk sends key presses to the focus window
        dialog.update()
        dialog.focus_get().event_generate("<Escape>")
        self.root.update()
        self.assertFalse(dialog.winfo_exists())
        self.assertTrue(hx_ui_common.HxSettings.load(services.local).floor_board_at_startup)

    def test_missing_screen_is_kept_and_open_now(self):
        services = self.make()
        services.settings.floor_board_screen = 2                          # that screen isn't connected now
        with mock.patch.object(hx_win, "monitors", return_value=[]):
            dialog = UserSettingsDialog(self.root, services)
        self.assertEqual(dialog.screen_box.key(), "0")
        self.assertEqual(dialog.values().floor_board_screen, 2)
        # Open the floor board now: saved first, then opened (an open one is opened again)
        with mock.patch.object(hx_win, "monitors", return_value=[]):
            services.nav.open_floor_board()
            first = services.nav.window("floor")
            dialog.size_var.set("window")
            dialog.width_var.set("800")
            dialog.height_var.set("600")
            dialog.open_button.invoke()
        floor = services.nav.window("floor")
        self.assertIsNot(floor, first)
        self.assertEqual(floor.geometry_spec, "800x600+0+0")
        self.assertFalse(dialog.winfo_exists())
        self.assertEqual(hx_ui_common.HxSettings.load(services.local).floor_board_size, "800x600")

    def test_failed_save_keeps_the_dialog(self):
        services = self.make()
        with mock.patch.object(hx_win, "monitors", return_value=[]):
            dialog = UserSettingsDialog(self.root, services)
        with mock.patch.object(services.local, "write_user_json", side_effect=OSError("disk full")):
            dialog.save_button.invoke()
        self.assertTrue(dialog.winfo_exists())
        self.assertEqual(dialog.problem_label.cget("text"), "Stripes hit a problem: OSError: disk full")
        self.assertFalse(dialog.save_button.instate(["disabled"]))


class NavigatorTests(UiCase):
    def test_roles_refused(self):
        services = self.make("jsmith", admin=False)
        nav = services.nav
        nav.open_admin()
        nav.open_test_print(film_id=self.world.ids["film"])
        self.assertEqual([c[0][1].text for c in self.tell.call_args_list],
                         ["Only Stripes Administrators can do this."] * 2)
        self.tell.reset_mock()
        services = self.make("akim", admin=True, sender=False)
        nav = services.nav
        nav.open_send()
        nav.stop(["mc_1"])
        nav.resume(["mc_1"])
        nav.set_clocks(["ln_1"])
        self.assertEqual([c[0][1].text for c in self.tell.call_args_list],
                         ["Only Stripes MapleJet Senders can do this."] * 4)
        self.assertEqual(hx_ui_common.open_windows(), [])
        with self.assertRaises(ValueError):
            nav.open_test_print(film_id=self.world.ids["film"], kind="spacing")

    def test_open_send_and_one_window_per_key(self):
        # Stand-ins for hx_ui_send's windows, so the Navigator's own rules are checked on their own
        # (RealWindowTests opens the real ones)
        made = []

        class SendWindow(hx_ui_common.HxWindow):
            key = "send"

            def __init__(self, master, services, *, prefill=None, line_ids=None):
                super().__init__(master, services)
                self.page, self.chosen, self.filled, self.ticked = "choose", False, [], []
                made.append((prefill, line_ids))

            def has_choices(self):
                return self.chosen

            def fill(self, request):
                self.filled.append(request)

            def tick_lines(self, line_ids):
                self.ticked.append(line_ids)

        class StopDialog(hx_ui_common.HxWindow):
            def __init__(self, master, services, *, machine_ids, reason="user", send_id=""):
                self.key = "stop:" + ",".join(sorted(machine_ids))
                super().__init__(master, services)
                self.args = (machine_ids, reason, send_id)

        class FailureWindow(hx_ui_common.HxWindow):
            def __init__(self, master, services, result, plan):
                self.key = f"failure:{result.send_id}"
                super().__init__(master, services)

        fake = mock.Mock(SendWindow=SendWindow, StopDialog=StopDialog, FailureWindow=FailureWindow)
        services = self.make()
        nav = services.nav
        with mock.patch.dict("sys.modules", {"hx_ui_send": fake}):
            nav.open_send()
            window = nav.window("send")
            nav.open_send()                                          # open: shown, nothing asked
            self.assertEqual(made, [(None, None)])
            nav.open_send(line_ids=["ln_1"])                         # no product chosen: no question
            self.assertEqual(window.ticked, [["ln_1"]])
            self.ask.assert_not_called()
            window.chosen = True
            self.ask.return_value = False
            request = SendRequest(send_id="sd_1", sender="EXAMPLE\\jsmith", computer="FLOOR01", created_at=0.0,
                                  lines=[])
            nav.open_send(prefill=request)                           # "Keep them"
            self.assertEqual(window.filled, [])
            self.assertEqual(self.ask.call_args[0][1].text, "Replace the choices in the Send window?")
            self.ask.return_value = True
            nav.open_send(prefill=request)                           # "Replace them"
            self.assertEqual(window.filled, [request])
            window.page = "countdown"
            nav.open_send(prefill=request)
            self.assertEqual(self.tell.call_args[0][1].text, "A Send is running. Wait for it to finish.")
            self.assertEqual(window.filled, [request])
            self.assertEqual(len(made), 1)
            nav.stop(["mc_2", "mc_1"], reason="rejected", send_id="sd_1")
            nav.stop(["mc_1", "mc_2"])
            dialogs = [w for w in hx_ui_common.open_windows() if isinstance(w, StopDialog)]
            self.assertEqual([d.args for d in dialogs], [(["mc_2", "mc_1"], "rejected", "sd_1")])
            result = mock.Mock(send_id="sd_9")
            nav.open_failure(result, None)
            nav.open_failure(result, None)
            self.assertEqual(len([w for w in hx_ui_common.open_windows() if isinstance(w, FailureWindow)]), 1)
            nav.open_result("sd_9")
            fake.ResultWindow.from_log.assert_called_once_with(self.root, services, "sd_9")

    def test_offline_send_recorded(self):
        services = self.make()
        nav = services.nav
        w = self.world
        mid = w.ids["machine1"]
        choice = LineChoice(line_id=w.ids["line"], machine_ids=[mid], product_id=w.ids["product"],
                            film_id=w.ids["film"], print_text_id=w.ids["print_text"], lot="4417", countries=["USA"])
        request = SendRequest(send_id=new_id("sd"), sender=services.identity.user, computer=services.identity.computer,
                              created_at=time.time(), lines=[choice])
        plan = merge_plan(request, services.data.current, services.identity)
        services.store.fs.down = True
        with contextlib.redirect_stderr(io.StringIO()):         # hx_send says what it couldn't write
            result = services.pipeline().run(plan, lambda event: None)
        self.assertTrue(result.machines[0].ok)
        self.assertIn("no_lock", result.machines[0].notes)
        # What the Send window does when a result has a no_lock machine (ui.md §5.8)
        nav.note_offline_send(result.send_id)
        nav.note_offline_send(result.send_id)
        self.assertEqual(nav.offline_sends, [result.send_id])
        nav.note_offline_answer(result.send_id, [mid], True)
        services.store.fs.down = False
        services.store.probe_now()
        self.pump(lambda: services.store.available)
        fake = mock.Mock()
        fake.record_offline.return_value = [mid]
        with mock.patch.object(services, "pipeline", return_value=fake):
            # A failure keeps the id for the next time the share comes back
            fake.record_offline.side_effect = ShareUnavailable(Msg("The server can't be reached."))
            services.bus.post(ShareHealth(ok=True, last_reached=time.time()))
            services.bus.pump()
            self.assertEqual(nav.offline_sends, [result.send_id])
            fake.record_offline.side_effect = None
            fake.record_offline.reset_mock()
            services.bus.post(ShareHealth(ok=False, last_reached=None))
            services.bus.post(ShareHealth(ok=True, last_reached=time.time()))
            services.bus.pump()
            self.assertEqual(fake.record_offline.call_count, 1)
            entry = fake.record_offline.call_args[0][0]
            self.assertEqual((entry["type"], entry["send_id"]), ("send", result.send_id))
            fake.confirm.assert_called_once_with(result.send_id, [mid], True)
            self.assertEqual(nav.offline_sends, [])
            self.assertEqual(nav.offline_answers, {})
            services.bus.post(ShareHealth(ok=True, last_reached=time.time()))
            services.bus.pump()
            self.assertEqual(fake.record_offline.call_count, 1)        # once only


    def offline_case(self):
        services = self.make()
        services.bg = DeferredBackground()
        fake = mock.Mock()
        fake.record_offline.return_value = ["mc_1"]
        for patcher in (mock.patch.object(services, "pipeline", return_value=fake),
                        mock.patch.object(services.data, "find_send",
                                          side_effect=lambda send_id: {"type": "send", "send_id": send_id})):
            patcher.start()
            self.addCleanup(patcher.stop)
        return services, services.nav, fake

    def test_offline_answer_given_during_the_run(self):
        services, nav, fake = self.offline_case()
        nav.note_offline_send("sd_x")
        services.bus.post(ShareHealth(ok=True, last_reached=time.time()))
        services.bus.pump()
        self.assertEqual(fake.record_offline.call_count, 1)        # the run's work is done, its done() isn't
        fake.confirm.assert_not_called()
        # Meanwhile the result window's confirm hit a ShareError and hands the answer over
        nav.note_offline_answer("sd_x", ["mc_1"], False)
        self.assertEqual(fake.record_offline.call_count, 1)        # one run at a time
        done, result, error = services.bg.pending.pop(0)
        done(result, error)
        # The first run had no answer: the Send stays and runs again for its answer
        fake.confirm.assert_called_once_with("sd_x", ["mc_1"], False)
        self.assertEqual(fake.record_offline.call_count, 2)
        self.assertEqual((nav.offline_sends, nav.offline_answers), (["sd_x"], {"sd_x": (["mc_1"], False)}))
        services.bg.finish()
        self.assertEqual((nav.offline_sends, nav.offline_answers), ([], {}))
        self.assertEqual(fake.confirm.call_count, 1)

    def test_offline_answer_after_the_run(self):
        services, nav, fake = self.offline_case()
        # The share came back and the run dropped the Send before the answer's ShareError arrived
        nav.note_offline_send("sd_y")
        services.bus.post(ShareHealth(ok=True, last_reached=time.time()))
        services.bus.pump()
        services.bg.finish()
        self.assertEqual(nav.offline_sends, [])
        nav.note_offline_answer("sd_y", ["mc_1"], True)            # the share is up: recorded now
        services.bg.finish()
        fake.confirm.assert_called_once_with("sd_y", ["mc_1"], True)
        self.assertEqual((nav.offline_sends, nav.offline_answers), ([], {}))
        # With the share down the answer waits for ShareHealth(ok=True)
        nav.note_offline_send("sd_z")
        with mock.patch.object(services.store, "available", False):
            nav.note_offline_answer("sd_z", ["mc_1"], False)
        self.assertEqual(fake.confirm.call_count, 1)
        self.assertEqual(nav.offline_sends, ["sd_z"])
        services.bus.post(ShareHealth(ok=True, last_reached=time.time()))
        services.bus.pump()
        services.bg.finish()
        self.assertEqual(fake.confirm.call_args, mock.call("sd_z", ["mc_1"], False))
        self.assertEqual((nav.offline_sends, nav.offline_answers), ([], {}))


class RealWindowTests(UiCase):
    """The Navigator with hx_ui_send's and hx_ui_admin's own windows (ui.md §15.3). Their tests use a
    stub Navigator, so this is where the two halves of the UI meet."""

    def tearDown(self):
        super().tearDown()
        for fake in self.world.printers:
            self.assertLessEqual(set(fake.writes), set(ALLOWED_WRITE_TAGS))
            self.assertLessEqual(fake.max_in_flight, 1)
            self.assertEqual(fake.violations, [])

    def request(self, lot="4417"):
        ids, identity = self.world.ids, self.services.identity
        choice = LineChoice(line_id=ids["line"], machine_ids=[ids["machine1"]], product_id=ids["product"],
                            film_id=ids["film"], print_text_id=ids["print_text"], lot=lot, countries=["USA", "Canada"])
        return SendRequest(send_id=new_id("sd"), sender=identity.user, computer=identity.computer,
                           created_at=time.time(), lines=[choice])

    def only(self, cls):
        """The one open window of that class."""
        found = [w for w in hx_ui_common.open_windows() if isinstance(w, cls)]
        self.assertEqual(len(found), 1, found)
        return found[0]

    def test_send_window(self):
        services = self.make()
        nav, lid = services.nav, self.world.ids["line"]
        nav.open_send(line_ids=[lid])
        send = nav.window("send")
        self.assertIsInstance(send, hx_ui_send.SendWindow)
        self.assertEqual((list(send.blocks), send.has_choices()), ([lid], False))
        nav.open_send()                                             # open: shown, nothing asked
        self.assertIs(self.only(hx_ui_send.SendWindow), send)
        # No product chosen yet: the prefill goes in without a question (ui.md §5.1 item 2)
        request = self.request()
        nav.open_send(prefill=request)
        self.ask.assert_not_called()
        self.assertTrue(send.has_choices())
        self.assertEqual(send.blocks[lid].lot_entry.get(), "4417")
        # A product chosen: "Keep them", then "Replace them"
        self.ask.return_value = False
        nav.open_send(line_ids=[lid])
        self.assertEqual(self.ask.call_args[0][1].text, "Replace the choices in the Send window?")
        self.assertEqual(self.ask.call_args[1], {"yes": "Replace them", "no": "Keep them"})
        self.assertEqual(send.blocks[lid].lot_entry.get(), "4417")
        self.ask.return_value = True
        nav.open_send(line_ids=[lid])
        self.assertEqual((list(send.blocks), send.has_choices()), ([lid], False))
        self.assertIs(self.only(hx_ui_send.SendWindow), send)
        # A Send running (a pipeline held in its countdown): the window is only shown (item 3) ...
        nav.open_send(prefill=request)
        release = threading.Event()
        self.addCleanup(release.set)

        class Held:
            def run(self, plan, on_event, cancel=None):
                release.wait(10)
        self.patch(services, "pipeline", return_value=Held())
        self.assertTrue(send.send_button.instate(["!disabled"]))
        send.send_button.invoke()
        self.assertEqual(send.page, "countdown")
        self.ask.reset_mock()
        nav.open_send(line_ids=[lid])
        self.assertEqual(self.tell.call_args[0][1].text, "A Send is running. Wait for it to finish.")
        self.ask.assert_not_called()
        self.assertEqual(send.blocks[lid].lot_entry.get(), "4417")
        # ... and quitting asks, naming it (ui.md §15.4 item 1)
        self.ask.return_value = False
        self.assertFalse(hx_ui.can_quit(self.root, services))
        self.assertEqual(hx_ui_common.text(self.ask.call_args[0][1]),
                         "A Send to Line 1 Bagger 1 is still running. If you quit now, a printer may be left halfway "
                         "through. Quit anyway?")
        # Refused before any printer request: back to the choices, and nothing left to ask about
        release.set()
        services.bus.post(WorkFailed(ref=send.plan.send_id, msg=Msg("The templates changed since you checked them. "
                                                                    "Look at the Send window again.")))
        self.pump(lambda: send.page == "choose" and not hx_ui_common.running())
        self.ask.reset_mock()
        self.assertTrue(hx_ui.can_quit(self.root, services))
        self.ask.assert_not_called()
        self.assertEqual(self.world.printers[0].writes, [])

    def test_result_and_failure_windows(self):
        services = self.make()
        nav, mid = services.nav, self.world.ids["machine1"]
        # Reopened from a card: ResultWindow.from_log, one window per Send
        nav.open_result("sd_unknown")
        result_window = nav.window("result:sd_unknown")
        self.assertIsInstance(result_window, hx_ui_send.ResultWindow)
        self.assertEqual(result_window.pending, [])
        nav.open_result("sd_unknown")
        self.assertIs(self.only(hx_ui_send.ResultWindow), result_window)
        # The red window: one per Send
        plan = merge_plan(self.request(), services.data.current, services.identity)
        now = time.time()
        failed = SendResult(send_id=plan.send_id, sender=services.identity.user, computer=services.identity.computer,
                            started_at=now, finished_at=now, time_source="server",
                            machines=[MachineResult(machine_id=mid, colour=RED, ok=False, step=STEP_VERIFY_IDLE,
                                                    msg=Msg("The new message didn't read back right."))])
        nav.open_failure(failed, plan)
        red = nav.window(f"failure:{plan.send_id}")
        self.assertIsInstance(red, hx_ui_send.FailureWindow)
        self.assertEqual([row.machine_id for row in red.rows], [mid])
        nav.open_failure(failed, plan)
        self.assertIs(self.only(hx_ui_send.FailureWindow), red)

    def test_action_dialogs(self):
        services = self.make()
        nav, ids = services.nav, self.world.ids
        mid, lid = ids["machine1"], ids["line"]
        nav.stop([mid])
        stop = nav.window(f"stop:{mid}")
        self.assertIsInstance(stop, hx_ui_send.StopDialog)
        self.assertEqual((stop.machine_ids, stop.reason, stop.send_id, stop.state), ([mid], "user", "", "ask"))
        nav.stop([mid], reason="rejected", send_id="sd_1")          # open: shown as it is
        self.assertIs(self.only(hx_ui_send.StopDialog), stop)
        self.assertEqual(stop.reason, "user")
        nav.resume([mid])
        resume = nav.window(f"resume:{mid}")
        self.assertIsInstance(resume, hx_ui_send.ResumeDialog)
        self.assertEqual(resume.machine_ids, [mid])
        nav.resume([mid])
        self.assertIs(self.only(hx_ui_send.ResumeDialog), resume)
        nav.set_clocks([lid])
        clocks = nav.window(f"clocks:{lid}")
        self.assertIsInstance(clocks, hx_ui_send.SetClocksDialog)
        self.assertEqual((clocks.line_ids, clocks.machine_ids, clocks.state), ([lid], [mid], "ask"))
        nav.set_clocks([lid])
        self.assertIs(self.only(hx_ui_send.SetClocksDialog), clocks)
        # Without the sender role nothing opens
        plain = self.make("akim", sender=False, admin=True)
        for call in (lambda: plain.nav.stop([mid]), lambda: plain.nav.resume([mid]),
                     lambda: plain.nav.set_clocks([lid]), lambda: plain.nav.open_send()):
            call()
        self.assertEqual(len(hx_ui_common.open_windows()), 3)
        self.assertEqual([c[0][1].text for c in self.tell.call_args_list[-4:]],
                         ["Only Stripes MapleJet Senders can do this."] * 4)

    def test_admin_and_test_print_windows(self):
        services = self.make()
        nav, ids = services.nav, self.world.ids
        nav.open_admin(page="machines", record_id=ids["machine1"])
        admin = nav.window("admin")
        self.assertIsInstance(admin, hx_ui_admin.AdminWindow)
        self.assertIs(admin.current_page(), admin.pages["machines"])
        self.assertEqual(admin.pages["machines"].working.id, ids["machine1"])
        nav.open_admin(page="lines", record_id=ids["line"])        # open: shown, then select(page, record_id)
        self.assertIs(self.only(hx_ui_admin.AdminWindow), admin)
        self.assertIs(admin.current_page(), admin.pages["lines"])
        self.assertEqual(admin.pages["lines"].working.id, ids["line"])
        nav.open_admin()                                            # no page: stays where it is
        self.assertIs(admin.current_page(), admin.pages["lines"])
        nav.open_test_print(film_id=ids["film"], machine_id=ids["machine1"])
        test = nav.window(f"test:{ids['film']}")
        self.assertIsInstance(test, hx_ui_admin.TestPrintWindow)
        self.assertEqual(test.machine_box.record_id(), ids["machine1"])
        nav.open_test_print(film_id=ids["film"])
        self.assertIs(self.only(hx_ui_admin.TestPrintWindow), test)
        with self.assertRaises(ValueError):                         # calibration prints run from CalibrationWindow
            nav.open_test_print(film_id=ids["film"], kind="head_distance")
        self.assertIs(self.only(hx_ui_admin.TestPrintWindow), test)

    def test_tab_and_card_buttons(self):
        services = self.make()
        nav, ids = services.nav, self.world.ids
        mid, lid = ids["machine1"], ids["line"]
        tab = hx_ui.MapleJetTab(self.root, services)
        tab.send_button.invoke()
        send = nav.window("send")
        self.assertIsInstance(send, hx_ui_send.SendWindow)
        tab.clocks_button.invoke()
        self.assertEqual(nav.window(f"clocks:{lid}").line_ids, [lid])
        tab.admin_button.invoke()
        admin = nav.window("admin")
        self.assertIsInstance(admin, hx_ui_admin.AdminWindow)
        # A card's buttons open the same windows: one per key
        services.bus.post(BoardUpdate(board=board(line(lid, "Line 1", [view(mid, line=lid, can_stop=True)]))))
        services.bus.pump()
        card = tab.board.cards[lid]
        card.rows[mid].stop_button.invoke()
        self.assertIsInstance(nav.window(f"stop:{mid}"), hx_ui_send.StopDialog)
        card.send_button.invoke()
        self.assertIs(self.only(hx_ui_send.SendWindow), send)
        self.assertEqual(list(send.blocks), [lid])
        card.clocks_button.invoke()
        self.assertEqual(len([w for w in hx_ui_common.open_windows() if isinstance(w, hx_ui_send.SetClocksDialog)]), 1)
        # "Set up..." on a machine without a printer: that machine on the admin window's Machines page
        services.bus.post(BoardUpdate(board=board(line(lid, "Line 1", [
            view(mid, line=lid, colour=GREY, code="not_set_up", headline="No printer set up", monitored=False,
                 age=None, origin="")]))))
        services.bus.pump()
        tab.board.cards[lid].rows[mid].setup_button.invoke()
        self.assertIs(self.only(hx_ui_admin.AdminWindow), admin)
        self.assertIs(admin.current_page(), admin.pages["machines"])
        self.assertEqual(admin.pages["machines"].working.id, mid)
        # Every open window follows the language; the tab's too
        titles = {hx_ui_send.SendWindow: "MapleJet Send", hx_ui_send.SetClocksDialog: "Set clocks",
                  hx_ui_send.StopDialog: "Stop printing", hx_ui_admin.AdminWindow: "MapleJet admin"}
        windows = [(w, titles[type(w)]) for w in hx_ui_common.open_windows()]
        self.assertEqual(len(windows), 4)
        i18n.set_language("es")
        hx_ui.language_changed(services)
        for window, english in windows:
            self.assertNotEqual(i18n.tr(english), english)
            self.assertEqual(window.title(), i18n.tr(english))
        self.assertEqual(tab.send_button.cget("text"), "Enviar...")
        i18n.set_language("en")
        hx_ui.language_changed(services)
        self.assertEqual([w.title() for w, _ in windows], [english for _, english in windows])

    def send_and_wait(self, request):
        """A Send through the Navigator's Send window; returns its result window."""
        nav = self.services.nav
        nav.open_send(prefill=request)
        send = nav.window("send")
        self.assertTrue(send.send_button.instate(["!disabled"]))
        send.send_button.invoke()
        send_id = send.plan.send_id
        self.pump(lambda: nav.window(f"result:{send_id}") is not None)
        return nav.window(f"result:{send_id}")

    def test_offline_send_recorded_through_the_windows(self):
        # ui.md §15.3 item 3 end to end: the Send window notes the Send, the result window hands over
        # the answer it couldn't save, and the share's return records both with the real pipeline
        services = self.make()
        nav, ids = services.nav, self.world.ids
        mid, lid = ids["machine1"], ids["line"]
        # A first Send with the share up, so this PC knows the machine's state before the next one
        # (send.md §10.3 step 3; without it record_offline can't tell that nobody changed it, §14.1)
        services.store.write_poll(PollStatus(machine_id=mid, printer_id=ids["printer1"],
                                             poller=services.identity.key, counter=1))     # no handshake wait
        first = self.send_and_wait(self.request(lot="4416"))
        first.right_buttons[lid].invoke()
        self.assertTrue(services.store.read_expected(mid).confirmed)
        self.assertEqual(nav.offline_sends, [])
        first.close()
        # The next one with the share down
        nav.open_send(prefill=self.request())
        send = nav.window("send")
        services.store.fs.down = True
        with contextlib.redirect_stderr(io.StringIO()):          # hx_send says what it couldn't write
            send.send_button.invoke()
            send_id = send.plan.send_id
            self.pump(lambda: nav.window(f"result:{send_id}") is not None)
        self.assertEqual(nav.offline_sends, [send_id])
        result_window = nav.window(f"result:{send_id}")
        self.assertIn("no_lock", result_window.result.machines[0].notes)
        self.assertEqual(result_window.pending, [mid])
        with contextlib.redirect_stderr(io.StringIO()):
            result_window.right_buttons[lid].invoke()
        self.assertEqual(result_window.line_state[lid], "pending")         # not saved ...
        self.assertEqual(nav.offline_answers, {send_id: ([mid], True)})    # ... but kept for the share's return
        self.assertEqual(nav.offline_sends, [send_id])
        services.store.fs.down = False
        services.store.probe_now()
        self.pump(lambda: services.store.available)
        services.bus.post(ShareHealth(ok=True, last_reached=time.time()))
        services.bus.pump()
        self.assertEqual((nav.offline_sends, nav.offline_answers), ([], {}))
        state = services.store.read_expected(mid)
        self.assertEqual((state.send_id, state.mode, state.result, state.notes), (send_id, "running", AMBER, ["no_lock"]))
        self.assertEqual(state.origin_line, "Product of USA and Canada")
        self.assertEqual((state.confirmed, state.confirmed_by), (True, services.identity.user))
        # Once recorded, another ShareHealth does nothing
        with mock.patch.object(services, "pipeline") as pipeline:
            services.bus.post(ShareHealth(ok=True, last_reached=time.time()))
            services.bus.pump()
        pipeline.assert_not_called()


class ServiceTests(UiCase):
    def test_make_services(self):
        services = self.make()
        self.assertEqual(services.store.root, self.world.share_root)
        self.assertIs(services.nav.services, services)
        self.assertIsNone(services.nav.sounder)
        self.assertEqual(services.settings, hx_ui_common.HxSettings())
        self.assertFalse(services.started)
        # The time servers follow the company record
        company = services.data.current.company
        self.assertEqual(services.time._hosts, [h for h in (company.time_server, company.time_server_fallback) if h])
        for made in (services.pipeline(), services.actions()):
            self.assertIs(made.store, services.store)
        self.assertIsNot(services.pipeline(), services.pipeline())
        # Nothing was started: stopping only silences
        with mock.patch.object(hx_win, "stop_alert") as stop_alert, \
                mock.patch.object(services.poller, "stop") as poller_stop:
            hx_ui.stop_services(services, timeout_s=0.5)
        stop_alert.assert_called_once_with()
        poller_stop.assert_not_called()

    def test_start_and_stop_services(self):
        identity = self.world.identity("jsmith", admin=True, sender=True)
        env = {"STRIPES_HX_USER": identity.user, "STRIPES_HX_COMPUTER": identity.computer,
               "STRIPES_HX_SIDS": ",".join(sorted(identity.sids))}
        # UiCase keeps the collector off; on here (with nothing left to collect) so start_services is
        # seen turning it off
        self.addCleanup(gc.disable)
        gc.collect()
        gc.enable()
        # The real start_services wiring and threads, at scaled timings (architecture §9) so the first
        # board doesn't take a real watch_s
        scaled = functools.partial(hx_ui.make_services, timings=SCALED)
        with mock.patch.dict(os.environ, env), mock.patch.object(hx_win, "stop_alert") as stop_alert, \
                mock.patch.object(hx_ui, "make_services", scaled):
            services = hx_ui.start_services(self.root, config_path=None, bg=SyncBackground(), version="test")
            self.assertIs(services.timings, SCALED)
            self.services = services
            self.assertTrue(services.started)
            self.assertFalse(gc.isenabled())
            self.assertIsNotNone(services.nav.sounder)
            self.assertEqual(services.identity.user, "EXAMPLE\\jsmith")
            self.pump(lambda: services.data.current is not None and services.watcher.latest is not None
                      and services.watcher.latest.lines)
            names = {t.name for t in threading.enumerate()}
            self.assertTrue({"hx-data", "hx-poller", "hx-watch"} <= names, names)
            # The 100 ms bus pump runs on Tk's loop, and every BoardUpdate re-checks the monitor
            # flag (which notices the main window hidden to the tray)
            with mock.patch.object(services.nav, "update_monitor_flag") as flag:
                services.bus.post(BoardUpdate(board=board()))
                deadline = time.monotonic() + 5.0
                while not flag.called and time.monotonic() < deadline:
                    self.root.update()
                    time.sleep(0.01)
            self.assertTrue(flag.called)
            started = time.monotonic()
            hx_ui.stop_services(services, timeout_s=2.0)
            self.assertLess(time.monotonic() - started, 3.0)
            self.assertGreaterEqual(stop_alert.call_count, 1)
        for name in ("hx-data", "hx-poller", "hx-watch"):
            self.assertFalse(any(t.name == name and t.is_alive() for t in threading.enumerate()), name)

    def test_startup(self):
        services = self.make()
        with mock.patch.object(self.root, "after") as after:
            hx_ui.startup(self.root, services)
            after.assert_not_called()
            services.settings.floor_board_at_startup = True
            hx_ui.startup(self.root, services)
        after.assert_called_once_with(1000, services.nav.open_floor_board)
        with mock.patch.object(services.nav, "open_monitor") as open_monitor:
            hx_ui.open_monitor(self.root, services)
        open_monitor.assert_called_once_with()

    def test_language_changed_keeps_bag_text(self):
        services = self.make()
        tab = hx_ui.MapleJetTab(self.root, services)
        b = board(line("ln_1", "Line 1", [view("mc_1", origin="Product of USA and Canada"),
                                          view("mc_2", origin="USA (pre-printed)")]))
        services.watcher.latest = b
        services.bus.post(BoardUpdate(board=b))
        services.bus.pump()
        with mock.patch.object(hx_win, "monitors", return_value=[]):
            services.nav.open_floor_board()
        floor = services.nav.window("floor")

        def bag_texts():
            out = [row.origin.get() for card in tab.board.cards.values() for row in card.rows.values()]
            out += [row.origin.itemcget(row.origin.text_item, "text") for card in tab.board.cards.values()
                    for row in card.rows.values()]
            out += [floor.canvas.itemcget(i, "text") for i in floor.origin_items.values()]
            return out
        before = bag_texts()
        self.assertEqual(before[:2], ["Product of USA and Canada", "USA (pre-printed)"])
        i18n.set_language("es")
        hx_ui.language_changed(services)
        floor = services.nav.window("floor")
        self.assertEqual(bag_texts(), before)
        self.assertEqual(tab.send_button.cget("text"), "Enviar...")
        self.assertTrue(tab.who_label.cget("text").startswith("Sesión iniciada como EXAMPLE\\jsmith. Puede enviar"))
        age = tab.board.cards["ln_1"].rows["mc_1"].age_label.cget("text")
        self.assertNotEqual(age, "checked 2 s ago")
        self.assertEqual(age, i18n.tr("checked {seconds} s ago", seconds=2))
        self.assertEqual(floor.canvas.itemcget(floor.status_items["mc_1"], "text"), "Imprimiendo")
        self.assertEqual(floor.header.itemcget(floor.title_item, "text"), "Tablero de planta MapleJet")
        i18n.set_language("en")
        hx_ui.language_changed(services)
        self.assertEqual(tab.send_button.cget("text"), "Send...")
        self.assertEqual(bag_texts(), before)


class SelfCheckTests(UiCase):
    """deploy.md §5: the MapleJet part of --self-check, run from source (test_deploy.py also runs it
    through stripes.self_check)."""

    def test_self_check_parts(self):
        self.assertEqual([name for name, _ in hx_ui._CHECKS],
                         ["modules", "hashes", "key", "http", "sntp", "files", "windows", "sound", "pictures", "tab"])
        for name, check in hx_ui._CHECKS:
            with self.subTest(check=name):
                check(self.root)
        self.assertEqual(hx_ui_common.open_windows(), [])

    def test_self_check_from_source(self):
        before = {t for t in threading.enumerate() if t.is_alive()}
        started = time.monotonic()
        hx_ui.self_check(self.root)
        self.assertLess(time.monotonic() - started, 15.0)
        self.assertEqual(hx_ui_common.open_windows(), [])
        left = [t.name for t in threading.enumerate() if t.is_alive() and t not in before
                and t.name.startswith("hx-selfcheck")]
        self.assertEqual(left, [])

    def test_frozen_exe_must_not_hold_the_fake(self):
        # Check 1, frozen: the fake printer isn't in the exe (pyinstaller.spec excludes it)
        real = importlib.util.find_spec
        with mock.patch.object(sys, "frozen", True, create=True):
            with self.assertRaises(RuntimeError) as caught:
                hx_ui._check_modules(self.root)
            self.assertIn("the fake printer is in the exe", str(caught.exception))
            with mock.patch.object(importlib.util, "find_spec",
                                   side_effect=lambda name, *a: None if name == "hx_fake" else real(name, *a)):
                hx_ui._check_modules(self.root)

    def windows_rename(self, refuse_always=False):
        """open_for_read and os.replace as on Windows, where os.replace (MoveFileExW) refuses a target that
        a reader still holds open, even one that shares delete. Returns the patches and the refusals."""
        held, refused = [], []
        real_open, real_replace = hx_win.open_for_read, os.replace

        class Reader:
            """open_for_read's file, holding its path until closed."""
            def __init__(self, path):
                self.f, self.key = real_open(path), os.path.abspath(path)
                held.append(self.key)

            def read(self):
                return self.f.read()

            def close(self):
                held.remove(self.key)
                self.f.close()

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.close()

        def replace(src, dst):
            if os.path.abspath(dst) in held or (refuse_always and "stripes-hx-swap-" in dst):
                refused.append(dst)
                raise PermissionError(13, "Access is denied", dst)
            return real_replace(src, dst)
        patches = contextlib.ExitStack()
        patches.enter_context(mock.patch.object(hx_win, "open_for_read", Reader))
        patches.enter_context(mock.patch.object(os, "replace", replace))
        return patches, held, refused

    def test_swap_check_on_windows_rename_rules(self):
        # deploy.md §5.6: a refused replace while the reader holds the file is fine, once it lets go the
        # replace must work
        patches, held, refused = self.windows_rename()
        with patches:
            hx_ui._check_files(self.root)
        self.assertEqual(len(refused), 1)
        self.assertEqual(held, [])
        with self.assertRaises(PermissionError), self.windows_rename(refuse_always=True)[0]:
            hx_ui._check_files(self.root)

    def test_self_check_names_the_failure(self):
        with mock.patch.object(hx_ui, "_CHECKS", (("sntp", hx_ui._check_sntp),)), \
                mock.patch("hx_time.sntp_query", side_effect=OSError("no time server")):
            with self.assertRaises(RuntimeError) as caught:
                hx_ui.self_check(self.root)
        self.assertIn("hx sntp", str(caught.exception))

    def test_self_check_restores_the_environment(self):
        before = {n: os.environ.get(n) for n in ("STRIPES_V2", "STRIPES_V2_ROAMING", "STRIPES_V2_LOCAL",
                                                 "STRIPES_V2_MACHINE")}
        hx_ui._check_tab(self.root)
        self.assertEqual({n: os.environ.get(n) for n in before}, before)


if __name__ == "__main__":
    unittest.main()
