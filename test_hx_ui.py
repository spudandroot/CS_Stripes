"""Tests for hx_ui and hx_ui_monitor (the MapleJet tab, services, line cards, monitor, floor board and
alert sound). Run: xvfb-run -a python -m unittest -v

The Tk tests need a display; they skip themselves when Tk can't start. The hx_ui_common tests are
in test_hx_ui_common.py; the Send and admin screens are tested in test_hx_ui_send.py and
test_hx_ui_admin.py.
"""

import contextlib
import dataclasses
import gc
import io
import os
import re
import shutil
import tempfile
import threading
import time
import tkinter as tk
import unittest
import wave
from unittest import mock

import hx_state
import hx_ui
import hx_ui_common
import hx_ui_monitor
import hx_win
import i18n
from hx_core import FakeClock, Msg
from hx_fake import FakeWorld
from hx_message import merge_plan
from hx_model import (AMBER, BLUE, GREEN, GREY, RED, TIMINGS, Alert, BoardUpdate, BoardView, LineChoice, LineView,
                      MachineView, PollerUpdate, SendRequest, ShareHealth, ShareUnavailable, new_id)
from hx_protocol import GateRegistry, release_key
from hx_ui_common import CARD_BG, COLOUR_HEX, DIM_FG, SyncBackground
from hx_ui_monitor import FloorBoard, LineBoard, MonitorWindow, UserSettingsDialog, board_summary, floor_layout

HERE = os.path.dirname(os.path.abspath(__file__))

# ui.md §17.1's scaled timings, with printer and share timeouts that leave room for a busy machine
SCALED = dataclasses.replace(TIMINGS.scaled(0.05), http_timeout_s=0.5, web_repeat_window_s=0.5, share_timeout_s=1.0)


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
        self.assertEqual((lay.bar_w, lay.names_w, lay.origin_w, lay.status_w), (58, 326, 864, 480))
        small = floor_layout(640, 360, 40)
        self.assertEqual((small.header_h, small.header_px, small.line_px, small.machine_px, small.origin_px,
                          small.status_px, small.age_px), (48, 22, 8, 9, 10, 9, 8))

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
    threads, and tell/ask patched on hx_ui_common."""
    root = None

    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
        except tk.TclError as e:
            raise unittest.SkipTest(f"Tk can't start: {e}")
        cls.root.withdraw()

    @classmethod
    def tearDownClass(cls):
        for window in hx_ui_common.open_windows():
            window.destroy()
        cls.root.destroy()

    def setUp(self):
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

    def open(self, services, **kw):
        with mock.patch.object(hx_win, "monitors", return_value=kw.pop("monitors", [])):
            fb = FloorBoard(self.root, services, **kw)
        fb.show(self.board())
        self.root.update_idletasks()
        return fb

    def test_floor_board(self):
        services = self.make()
        fb = self.open(services, size="800x600")
        self.assertEqual(fb.geometry_spec, "800x600+0+0")
        self.assertFalse(fb.full_screen)
        self.assertEqual(fb.title(), "MapleJet floor board")
        self.root.update()
        self.assertTrue(fb.geometry().startswith("800x600"))
        c = fb.canvas
        self.assertEqual([r.machine_id for r in fb.rows], ["mc_1", "mc_2", "mc_3", "mc_4"])
        self.assertEqual({mid: c.itemcget(item, "text") for mid, item in fb.origin_items.items()},
                         {r.machine_id: r.origin_line for r in fb.rows})
        self.assertEqual(c.itemcget(fb.unknown_items["mc_4"], "text"), "(origin unknown)")
        self.assertEqual(list(fb.unknown_items), ["mc_4"])
        self.assertEqual(c.itemcget(fb.status_items["mc_1"], "text"), "Printing")
        self.assertEqual(c.itemcget(fb.status_items["mc_2"], "fill"), COLOUR_HEX[RED])
        self.assertEqual(c.itemcget(fb.age_items["mc_1"], "text"), "checked 2 s ago")
        # NOT MONITORED: grey text, but the bar keeps its last colour
        for items in (fb.origin_items, fb.status_items, fb.age_items):
            self.assertEqual(c.itemcget(items["mc_3"], "fill"), DIM_FG)
        self.assertEqual(c.itemcget(fb.bar_items["mc_3"], "fill"), COLOUR_HEX[AMBER])
        # The bar of a machine with an unacknowledged sound alert flashes; the others don't
        self.assertEqual(c.itemcget(fb.bar_items["mc_2"], "fill"), COLOUR_HEX[RED])
        fb.flash()
        self.assertEqual((c.itemcget(fb.bar_items["mc_2"], "fill"), fb.flash_on), (CARD_BG, False))
        self.assertEqual(c.itemcget(fb.bar_items["mc_1"], "fill"), COLOUR_HEX[GREEN])
        fb.flash()
        self.assertEqual(c.itemcget(fb.bar_items["mc_2"], "fill"), COLOUR_HEX[RED])
        # Layout from the canvas's size
        self.assertEqual(fb.layout, floor_layout(800, 600, 4))
        # Acknowledge: shown to someone who may, while a floor alert rings; Refresh only standalone
        self.assertTrue(shown(fb.ack_button) and shown(fb.close_button))
        self.assertFalse(shown(fb.refresh_button))
        with mock.patch.object(services.watcher, "acknowledge") as ack:
            fb.ack_button.invoke()
        ack.assert_called_once_with(["al_f"])
        fb.destroy()
        plain = self.make("akim", sender=False, admin=False)
        fb = self.open(plain)
        self.assertFalse(shown(fb.ack_button))
        self.assertTrue(shown(fb.close_button))

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

    def test_ages_tick_and_empty_board(self):
        services = self.make()
        services.clock = FakeClock()
        fb = self.open(services, size="800x600")
        services.clock.advance(4.0)
        fb.tick()
        self.assertEqual(fb.canvas.itemcget(fb.age_items["mc_1"], "text"), "checked 6 s ago")
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
        with mock.patch.object(services.poller, "refresh_now", return_value=False):
            fb.refresh_button.invoke()
        self.assertEqual(fb.canvas.itemcget(fb.banner_item, "text"),
                         f"You can refresh once every {int(SCALED.refresh_now_min_s)} s.")

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

    def test_long_origin_shrinks_then_wraps(self):
        services = self.make()
        long_origin = "Product of " + ", ".join(["USA", "Canada", "Mexico"] * 12) + ", and Peru"
        b = board(line("ln_1", "Line 1", [view("mc_1", origin="Product of USA"), view("mc_2", origin=long_origin)]))
        with mock.patch.object(hx_win, "monitors", return_value=[]):
            fb = FloorBoard(self.root, services, size="800x600")
        fb.show(b)
        c = fb.canvas

        def px(mid):
            return int(re.search(r"-(\d+)", c.itemcget(fb.origin_items[mid], "font")).group(1))
        lay = fb.layout
        room = lay.origin_w - 16
        family = hx_ui_common.bag_font_family(fb)
        short = px("mc_1")                                                       # the largest size that fits
        self.assertLessEqual(fb._font(family, short, "bold").measure("Product of USA"), room)
        if short < lay.origin_px:
            self.assertGreater(fb._font(family, short + 1, "bold").measure("Product of USA"), room)
        self.assertEqual(px("mc_2"), max(8, round(lay.origin_px * 0.4)))         # shrunk as far as allowed ...
        self.assertEqual(int(float(c.itemcget(fb.origin_items["mc_2"], "width"))), lay.origin_w - 16)  # ... then wraps
        self.assertEqual(int(float(c.itemcget(fb.origin_items["mc_1"], "width"))), 0)
        self.assertEqual(c.itemcget(fb.origin_items["mc_2"], "text"), long_origin)    # never cut short


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
        # Stand-ins for hx_ui_send's windows (the real ones are tested in test_hx_ui_send.py), so the
        # Navigator's own rules are checked here
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
        services.bg.finish()
        # The first run had no answer: the Send stays and runs again for its answer
        fake.confirm.assert_called_once_with("sd_x", ["mc_1"], False)
        self.assertEqual(fake.record_offline.call_count, 2)
        self.assertEqual(nav.offline_sends, ["sd_x"])
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
        enabled = gc.isenabled()
        self.addCleanup(lambda: gc.enable() if enabled else None)
        with mock.patch.dict(os.environ, env), mock.patch.object(hx_win, "stop_alert") as stop_alert:
            services = hx_ui.start_services(self.root, config_path=None, bg=SyncBackground(), version="test")
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
        self.assertEqual(floor.canvas.itemcget(floor.title_item, "text"), "Tablero de planta MapleJet")
        i18n.set_language("en")
        hx_ui.language_changed(services)
        self.assertEqual(tab.send_button.cget("text"), "Send...")
        self.assertEqual(bag_texts(), before)


class SelfCheckTests(UiCase):
    """The checks of deploy.md §5.2 that don't need the send and admin screens (check 1 imports
    them; test_deploy.py runs the whole self-check)."""

    def test_self_check_parts(self):
        for name, check in hx_ui._CHECKS:
            if name == "modules":
                continue
            with self.subTest(check=name):
                check(self.root)
        self.assertEqual(hx_ui_common.open_windows(), [])

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
