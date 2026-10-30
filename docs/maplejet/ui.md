# MapleJet Hx build spec: ui

Part 6 of 7. This file specifies the five UI modules architecture.md gives to ui.md: `hx_ui_common`
(shared widgets and helpers), `hx_ui_send` (Send, result, red window, Stop, Resume, Set clocks),
`hx_ui_monitor` (line cards, MapleJet monitor, floor board, alert sound, per-user settings dialog),
`hx_ui_admin` (admin screens, film editor, print-text designer, test print, calibration) and `hx_ui`
(services, `Navigator`, the MapleJet tab, start-up and shut-down). It also fixes how the Zebra screens
move under the Zebra tab, the role gating, the UI threading rules, every UI text with its Spanish, and
`test_hx_ui.py`. Names, types, layers and threads come from architecture.md and are not changed; §1
lists the names this file adds inside its own modules.

Contents: 0 Scope and conventions · 1 Interfaces added here · 2 Rules for every MapleJet screen ·
3 The two tabs · 4 The MapleJet tab · 5 The Send window · 6 The result and confirmation window ·
7 The red failure window · 8 Something's wrong, Stop, Resume and Set clocks · 9 Line cards, the
MapleJet monitor and alerts · 10 The floor board · 11 Admin screens · 12 The test print window ·
13 The calibration window (OPEN) · 14 Per-user settings · 15 hx_ui: services, Navigator, start-up and
shut-down · 16 Texts and Spanish · 17 Tests · 18 OPEN items · 19 Decisions made in this spec

## 0. Scope and conventions

1. Authority: the plan, then architecture.md, then protocol.md, storage.md, states.md and send.md for
   their modules, then this file for the UI modules. The UI decides no colour, state, permission rule,
   origin wording or validation result: those come from `hx_state`, `hx_message`, `hx_share` and
   `hx_send` (architecture §5.10, §9.3). Where this file needs a small pure helper that no other file
   defines, it is defined here, in a UI module, and tested against the owner's function.
2. Examples follow the public-repo rule: host 192.0.2.10, UID 1109 with password 123321,
   `time.example.local` with fallback 192.0.2.123, "Example Co", "Example Co 2lb Onion", "Line 1"
   (code L1), "Line 1 Bagger 1" (code B1), film EXCO1 (Rev C), print text "Packed on + origin" (PKO),
   users `EXAMPLE\jsmith` and `EXAMPLE\akim`, PCs `FLOOR01` and `FLOOR02`.
3. Packaging doesn't change anything here. The output stays the single-file `Stripes.exe` built by
   PyInstaller, signed in CI and deployed as today (Group Policy copies it per user); no MSI, no
   installer (architecture §0.6). Nothing in the UI needs an installer: no registry writes, no Start
   menu entries, no Run key. "Open the floor board at startup" works through the logon shortcut IT
   already deploys (`Stripes.exe --tray`): when Stripes starts, `hx_ui.startup` opens the floor board.
   The alert sound is `assets/alert.wav`, bundled with the existing `assets` folder.
4. No new dependencies: `tkinter`/`ttk`, Pillow (`Image`, `ImageOps`, `ImageTk`, already used by
   `stripes.py`), and the standard library (`wave`, `struct`, `math`, `io`, `base64`, `weakref`,
   `threading`, `traceback`, `os` (`os.startfile`), `webbrowser`, `time`, `datetime`,
   `dataclasses`). Windows calls go through `hx_win` only.
5. Code style: flat modules at the repo root, a module docstring like the existing files, plain short
   comments, dataclasses, `tr()`/`N_()` from `i18n`, `ttk` widgets (plain `tk` widgets only where a
   background colour is needed), worker threads plus `after()` polling, and `unittest`.
6. Text sketches use `[Button]`, `[x]` checkbox, `( )`/`(•)` radio, `[value v]` combobox,
   `[______]` entry, `{BagText}` for a bag-text canvas and `{Picture}` for a `PictureView`.

## 1. Interfaces added here

Names that architecture §6.10 already defines are repeated only where this file adds to them or fixes
an unspecified constructor. Every UI module defines `ES = {...}` near its top and calls
`i18n.add_spanish(ES)` right after it (architecture §10.6).

### 1.1 hx_ui_common

```python
ES: dict[str, str]                                  # §16.1
COLOUR_HEX: dict[str, str]                          # architecture §6.10, exactly
PAGE_BG, CARD_BG, TEXT_FG, MUTED_FG, RULE_FG = "#f3f4f6", "#ffffff", "#111827", "#6b7280", "#d1d5db"
DIM_FG = "#9ca3af"                                  # NOT MONITORED text on the floor board
HIGHLIGHT_BG = "#fde68a"                            # an origin change in the Send and result windows
RED_BG, RED_FG = "#b91c1c", "#ffffff"               # the red windows (= COLOUR_HEX[RED])
UI_FONT = "Segoe UI"                                # Tk substitutes its default family off Windows
BAG_FONTS = ("Segoe UI", "Arial", "Liberation Sans", "DejaVu Sans")   # BagText: the first installed
MONO_FONTS = ("Consolas", "Courier New", "Liberation Mono", "DejaVu Sans Mono")
APP_TITLE = "Stripes"                               # title of tell/ask dialogs (a name, not translated)
STRICT_THREADS = False                              # tests set True (§2.1 rule 7)
ALERT_WAV = "assets/alert.wav"
PREVIEW_MAX_SIDE = 480                              # film photo previews (storage.md §18)

def on_tk_thread() -> bool                          # threading.current_thread() is threading.main_thread()
def check_tk_thread() -> None                       # RuntimeError("Tk call off the Tk thread") when
                                                    # STRICT_THREADS and not on_tk_thread()
def setup_styles(root: tk.Misc) -> None             # §2.4; idempotent
def text(msg: Msg | None) -> str                    # architecture §6.10
def when(epoch: float | None) -> str                # time.strftime("%Y-%m-%d %H:%M", time.localtime(epoch)); "" for None
def join_names(names: list[str]) -> str             # "A", "A and B", "A, B and C" (§2.2 rule 6)
def bag_font_family(root: tk.Misc) -> str           # first of BAG_FONTS in tkinter.font.families(), else "TkDefaultFont"
def mono_font_family(root: tk.Misc) -> str          # first of MONO_FONTS, else "TkFixedFont"

class Texts:
    """The fixed text of one window or frame: set once, re-applied in the new language by apply()."""
    def set(self, widget, text: str, *, option: str = "text", **values) -> None: ...
        # widget.configure(**{option: tr(text, **values)}); remembered under (str(widget), option)
    def msg(self, widget, msg: Msg | None, *, option: str = "text") -> None: ...   # text(msg), remembered
    def title(self, window: tk.Wm, text: str, **values) -> None: ...
    def tab(self, notebook: ttk.Notebook, tab_id, text: str, **values) -> None: ...
    def heading(self, tree: ttk.Treeview, column: str, text: str, **values) -> None: ...
    def forget(self, widget) -> None: ...           # drop every entry of that widget
    def apply(self) -> None: ...                    # re-run every entry whose widget still exists

class ChoiceBox(ttk.Combobox):
    """Read-only combobox of fixed choices, shown translated; the program sees keys."""
    def __init__(self, master, choices: list[tuple[str, str]], **kw): ...   # (key, N_ text)
    def key(self) -> str: ...                       # "" when nothing is chosen
    def set_key(self, key: str) -> None: ...
    def set_choices(self, choices: list[tuple[str, str]]) -> None: ...
    def retranslate(self) -> None: ...              # re-shows the values, keeps the chosen key

class RecordBox(ttk.Combobox):
    """Read-only combobox of records, shown by name (names are data: never translated)."""
    def set_records(self, records: list[tuple[str, str]], *, none_text: str = "") -> None: ...
        # (id, display name); none_text (an N_ text) adds a first entry meaning "" (e.g. "All machines")
    def record_id(self) -> str: ...
    def set_record_id(self, record_id: str) -> None: ...
    def retranslate(self) -> None: ...              # re-shows none_text in the current language

class BagText(tk.Canvas):                           # architecture §6.10, completed here
    hx_no_translate = True
    def __init__(self, master, *, size: int = 12, bold: bool = False, wrap_px: int | None = None, **kw): ...
        # kw may hold foreground (default TEXT_FG), background (default CARD_BG) and family
        # (default bag_font_family); they are taken out of kw, the rest goes to tk.Canvas
    def set(self, text: str) -> None: ...
    def get(self) -> str: ...
    def set_colours(self, *, foreground: str | None = None, background: str | None = None) -> None: ...
    text_item: int                                  # the canvas text item (tests read itemcget(text_item, "text"))

class PictureView(tk.Canvas):                       # architecture §6.10, completed here
    hx_no_translate = True
    def __init__(self, master, *, width: int = 512, **kw): ...   # height = round(width * 160 / 1024) + 2
    def set_bmp(self, bmp: bytes | None) -> None: ...
    bmp: bytes | None                               # what was last set

def bmp_to_image(bmp: bytes) -> "PIL.Image.Image"   # architecture §6.10
def png_preview(path: str) -> bytes | None          # §11.7.6; None when Pillow can't open the file
def photo_from_png(master, png: bytes, *, height: int | None = None,
                   box: tuple[int, int] | None = None) -> "ImageTk.PhotoImage"
    # scaled to height, or to fit inside box (width, height); either way its shape is kept
def fit_size(size: tuple[int, int], box: tuple[int, int]) -> tuple[int, int]   # size scaled to fit box
def alert_wav_bytes() -> bytes                      # §9.4: the exact bytes of assets/alert.wav

class Scroller(ttk.Frame):
    """A vertically scrolling area: put children in .inner. Mouse wheel scrolls it while the pointer is over it."""
    def __init__(self, master, *, background: str = PAGE_BG, **kw): ...
    inner: tk.Frame

class HxWindow(tk.Toplevel):                        # architecture §6.10, completed here
    hx_window = True                                # the Zebra shortcuts ignore these windows
    hx_no_translate = True                          # App._retranslate skips it; retranslate() does the work
    key: str = ""                                   # Navigator keeps one open window per key
    def __init__(self, master, services: "HxServices | None" = None, *, title: str = "", **kw): ...
    services: "HxServices | None"
    texts: Texts
    def subscribe(self, event_type: type, callback) -> None: ...   # bus.subscribe; unsubscribed in destroy()
    def later(self, ms: int, func) -> str: ...      # self.after(), cancelled in destroy()
    def retranslate(self) -> None: ...              # texts.apply(), retranslate() of every ChoiceBox/RecordBox
                                                    # child, then refresh_texts()
    def refresh_texts(self) -> None: ...            # subclasses re-render Msg-based content; base: nothing
    def close(self) -> None: ...                    # the close box calls this; base: destroy()
    def show(self) -> None: ...                     # deiconify, lift, topmost for 200 ms, focus_force
    def destroy(self) -> None: ...
def open_windows() -> list[HxWindow]                # every live HxWindow, in opening order

def tell(parent, msg: Msg, *, kind: str = "info") -> None          # architecture; kind "info"|"warning"|"error"
def ask(parent, msg: Msg, *, yes: str, no: str) -> bool            # architecture; §2.6

class SyncBackground:                               # architecture: run(work, done) on the calling thread

@dataclass(kw_only=True)
class WorkFailed:                                   # posted when a start_thread target raises
    ref: str                                        # the send_id, action_id or run_id
    msg: Msg
def start_thread(services, name: str, ref: str, target: Callable[[], object]) -> threading.Thread
    # §2.1 rule 4

def mark_running(key: str, what: Msg) -> None       # a Send, test or action that can_quit must ask about
def mark_done(key: str) -> None
def running() -> list[Msg]

@dataclass(frozen=True, kw_only=True)
class Roles:
    sender: bool = False
    admin: bool = False
    acknowledge: bool = False
def roles(services) -> Roles                        # §2.7

@dataclass(kw_only=True)
class HxSettings:                                   # architecture §6.10; fields in §14
    SIZE_PATTERN = r"([0-9]{3,5})x([0-9]{3,5})"
    MIN_WINDOW = (640, 360)
    def size_tuple(self) -> tuple[int, int] | None: ...   # None for "full"

PHOTO_SCREEN_SHARE = 0.9                            # PhotoWindow: at most 90% of the screen's width and height

class PhotoWindow(HxWindow):                        # a film photo, large (Send window and film editor), §5.11
    def __init__(self, master, services, *, png: bytes, caption: str,
                 original: FileRef | None = None): ...   # caption is data, not translated
    # key f"photo:{original.sha256}" when original is given
    def toggle_zoom(self, focus: tuple[float, float] = (0.5, 0.5)) -> None: ...
    def open_file(self) -> None: ...
    source: str                                     # "preview" | "original"
    full: bool                                      # True: full size; False: fitted to the window
def show_photo(master, services, *, png: bytes, caption: str,
               original: FileRef | None = None) -> PhotoWindow   # shows the photo's open window instead, if any
```

### 1.2 hx_ui_send

```python
ES: dict[str, str]                                  # §16.2
def origin_preview(film: Film, countries: list[str]) -> str
    # the origin line the Send window shows before the plan can be merged: film.origin_preprinted ->
    # f"{film.preprinted_origin} (pre-printed)"; else hx_message.origin_phrase(countries) when countries
    # isn't empty; else "". A test asserts it equals MachinePlan.origin_line for the same choice.
def placement_text(position: PositionWorking) -> Msg
    # what a sender reads (§5.4): "Delay {ms} ms" (ms = position.sent / 1000) on a delay machine,
    # "Print position {mm} mm" (mm = film_value + adjustment, from the bag's bottom edge) on an encoder one
def sorted_films(films: list[Film]) -> list[Film]   # by natural_key(code), natural_key(artwork_revision), id
THUMB_BOX = (100, 150)                              # film photo thumbnails fit this box (width, height)
def run_send(master, services, plan: SendPlan) -> "SendWindow | None"
    # Try again (§5.9): through the Navigator's one Send window; None when it couldn't start

class SendWindow(HxWindow):                         # key "send"
    def __init__(self, master, services, *, prefill: SendRequest | None = None,
                 line_ids: list[str] | None = None): ...
    def start(self, plan: SendPlan) -> None: ...    # leave the choice page and run the plan (§5.8)
    def fill(self, request: SendRequest) -> None: ...    # §5.10
    def tick_lines(self, line_ids: list[str]) -> None: ...
    def request(self) -> SendRequest: ...           # the choices as a request with a new send_id
    # attributes: §5.11

class LineBlock(ttk.Frame):                         # one chosen line in the Send window (§5.3)
    def __init__(self, master, window: SendWindow, line: Line): ...
    def choice(self) -> LineChoice: ...
    def fill(self, choice: LineChoice) -> None: ...
    def problems(self) -> list[Msg]: ...            # validate_choice(...) of choice()

class ResultWindow(HxWindow):                       # key f"result:{send_id}"
    def __init__(self, master, services, result: SendResult): ...
    @staticmethod
    def from_log(master, services, send_id: str) -> "ResultWindow | None": ...
        # None when a window for that send_id was already open (it is raised instead)
    request: SendRequest | None                     # for "Fix and send again"; set by SendWindow, or from the log
    def fill(self, result: SendResult, *, request: SendRequest | None = None,
             pending: list[str] | None = None) -> None: ...

class FailureWindow(HxWindow):                      # key f"failure:{send_id}"
    def __init__(self, master, services, result: SendResult, plan: SendPlan): ...

class StopDialog(HxWindow):                         # key "stop:" + ",".join(sorted(machine_ids))
    def __init__(self, master, services, *, machine_ids: list[str], reason: str = "user",
                 send_id: str = "", request: SendRequest | None = None): ...
class ResumeDialog(HxWindow):                       # key "resume:" + ...
    def __init__(self, master, services, *, machine_ids: list[str]): ...
class SetClocksDialog(HxWindow):                    # key "clocks:" + ",".join(sorted(line_ids))
    def __init__(self, master, services, *, line_ids: list[str]): ...
```

### 1.3 hx_ui_monitor

```python
ES: dict[str, str]                                  # §16.3
FLOOR_MIN_ROWS = 4                                  # a floor-board row is never taller than 1/4 of the rows area
FLOOR_MIN_ROW_PX = 44                               # nor shorter than this: past it the floor board scrolls
FLOOR_GROUP_SHARE = 0.5                             # a line's band is half a row tall
FLOOR_FLASH_MS = 1000

@dataclass(frozen=True, kw_only=True)
class FloorLayout:
    width: int; header_h: int; group_h: float; row_h: float; rows_h: int; scrolls: bool
    bar_w: int; names_w: int; origin_w: int; status_w: int; age_w: int
    header_px: int; line_px: int; machine_px: int; origin_px: int; status_px: int; age_px: int
def floor_layout(width: int, height: int, rows: int, lines: int = 0, scrollbar_w: int = 0) -> FloorLayout  # §10.3; pure
def fit_px(texts, measure, start: int, smallest: int, room: float) -> int   # §10.3; pure
def ellipsize(value: str, measure, room: float) -> str                    # §10.3; pure, never on bag text
def short_name(machine_name: str, line_name: str, code: str = "") -> str  # §10.3; pure
def board_summary(board: BoardView) -> list[Msg]    # "{n} red" ... by colour, worst first, non-zero only

class LineBoard(ttk.Frame):                         # architecture; §9.1
    def __init__(self, master, services): ...
    def show(self, board: BoardView) -> None: ...
    def retranslate(self) -> None: ...
    cards: dict[str, "LineCard"]
class LineCard(tk.Frame): ...                       # one line (§9.1)
class MachineRow(tk.Frame): ...                     # one machine inside a LineCard
class MonitorWindow(HxWindow): ...                  # key "monitor"
class FloorBoard(HxWindow): ...                     # key "floor"
class AlertSounder:                                 # architecture; §9.3
    def __init__(self, services): ...
    def evaluate(self, board: BoardView | None) -> None: ...
    def stop(self) -> None: ...
    playing: bool
class AlertPopup(HxWindow): ...                     # key "alerts"; §9.5
class UserSettingsDialog(HxWindow): ...             # key "settings"; §14
```

### 1.4 hx_ui_admin

```python
ES: dict[str, str]                                  # §16.4
PAGES = ("company", "lines", "machines", "printers", "products", "print_texts", "adjustments")

class AdminWindow(HxWindow):                        # key "admin"; architecture signature
    def select(self, page: str = "", record_id: str = "") -> None: ...
    pages: dict[str, "AdminPage"]
class AdminPage(ttk.Frame): ...                     # the list-plus-editor pattern of §11.1
class CompanyPage(AdminPage): ...
class LinesPage(AdminPage): ...
class MachinesPage(AdminPage): ...
class PrintersPage(AdminPage): ...
class ProductsPage(AdminPage): ...                  # products and their films
class PrintTextsPage(AdminPage): ...
class AdjustmentsPage(AdminPage): ...
class ProductEditor(ttk.Frame): ...
class FilmEditor(ttk.Frame): ...                    # architecture
class PrintTextEditor(ttk.Frame): ...               # architecture
class AdjustmentDialog(HxWindow): ...               # key f"adjust:{machine_id}__{film_id}"
    def __init__(self, master, services, *, machine_id: str, film_id: str): ...
class TestRunPanel(ttk.Frame): ...                  # progress of a TestPrintRun (§12.4); used by both windows
class TestPrintWindow(HxWindow): ...                # key f"test:{film_id}"; kind must be "test" (§12)
class CalibrationWindow(HxWindow): ...              # key f"calibration:{machine_id}"

# pure helpers (no Tk), tested in test_hx_ui.py
def parse_number(text: str, *, whole: bool = False) -> float | int | None
    # strip; "" -> None; one "," with no "." counts as the decimal point; raises ValueError otherwise
def printer_problems(printer: Printer, data: HxData) -> list[Msg]     # §11.5
def product_problems(product: Product, data: HxData) -> list[Msg]     # §11.6
def adjustment_problems(adj: MachineFilmAdjustment, data: HxData) -> list[Msg]   # §11.9
def notify_user_problems(text: str) -> list[Msg]                      # §11.3
def origin_rule_ok(film: Film, pt: PrintText) -> bool
    # pre-printed film: pt has no origin item; otherwise exactly one (send.md §2.4)
def print_text_choices(film: Film, data: HxData) -> list[PrintText]   # any status, origin_rule_ok, by name
def film_choices(pt: PrintText, data: HxData) -> list[Film]           # any status, origin_rule_ok, by product then code
def membership_saves(pt: PrintText, wanted: set[str], data: HxData) -> tuple[list[Film], list[Msg]]   # §11.8.5
def new_film(product_id: str) -> Film                                 # Film(id=new_id("fm"), product_id=..., code="")
def longest_values(pt: PrintText, company: Company, today: date) -> dict[str, str]   # §11.8.4
def preview_lines(pt: PrintText, values: dict[str, str]) -> list[str]                # §11.8.4
```

### 1.5 hx_ui

```python
ES: dict[str, str]                                  # §16.5

class Navigator:                                    # architecture methods, plus:
    def __init__(self, root: tk.Misc) -> None: ...
    services: "HxServices"                          # set by make_services right after HxServices is built
    tab_shown: bool                                 # the MapleJet tab is the selected top tab
    sounder: "AlertSounder | None"                  # set by start_services
    def window(self, key: str) -> HxWindow | None: ...   # the open window with that key
    def floor_board_open(self) -> bool: ...
    def update_monitor_flag(self) -> None: ...      # §15.3
    def note_offline_send(self, send_id: str) -> None: ...   # §15.3 item 3
    def note_offline_answer(self, send_id: str, machine_ids: list[str], everything_right: bool) -> None: ...

class MapleJetTab(ttk.Frame):                       # architecture; §4
    hx_no_translate = True
    texts: Texts
    board: LineBoard
    def retranslate(self) -> None: ...
```

`HxServices`, `make_services`, `start_services`, `stop_services`, `can_quit`, `startup`,
`open_monitor`, `language_changed` and `self_check` keep architecture's signatures; §15 specifies them.

### 1.6 stripes.py and i18n.py

Only what architecture §10 lists. ui.md uses its one allowance (architecture §10.2 rule 1): the
header subtitle becomes `tr("Zebra labels and MapleJet bag printing")`, with Spanish "Etiquetas
Zebra e impresión de bolsas MapleJet" added to `i18n.ES`. Both are specified here and written by
other packages (README.md "Work packages"): the subtitle in `stripes.py` by WP9, the `i18n.ES` pairs
in `i18n.py` by WP1; the UI modules' builders (WP7, WP8) edit neither file. The old key "Zebra label
printing" stays in `i18n.ES` (an unused entry is harmless). "MapleJet monitor" → "Monitor MapleJet"
is added to `i18n.ES` by architecture §10.6; the UI modules use the same pair.

## 2. Rules for every MapleJet screen

### 2.1 Threads

1. Only the Tk thread touches widgets, `PhotoImage`s and Tk variables, calls `bus.subscribe`,
   `bus.unsubscribe` and `bus.pump`, and runs every callback below (architecture §7.1).
2. Blocking calls never run on the Tk thread. Blocking means anything that may touch the share, a
   printer, the time server, the domain controller or a file on another PC:

   | Blocking (worker thread only) | Not blocking (Tk thread is fine) |
   |---|---|
   | `DataService.save`, `save_file`, `save_preview`, `preview_bytes`, `share_path`, `find_send`, `append_log`, `append_send_log`, `pending_logs` | `DataService.current`, `problems`, `too_new`, `can_send`, `reload` |
   | every `ShareStore` read and write (`read_preview`, `read_json`, ...) | `ShareStore.available`, `last_reached`, `probe_now` |
   | every `PrinterClient` call | `Poller.set_monitor_open`, `refresh_now`, `last_update`, `mode` |
   | `SendPipeline.run`, `confirm`, `leave_it`, `pending_confirmation` | `StatusWatcher.latest`, `refresh_now`, `acknowledge` |
   | `PrinterActions.stop`, `check_resume`, `resume`, `set_clocks`; `TestPrintRun.run` | every `hx_message`, `hx_state`, `hx_model` function and the pure helpers of §1.4 |
   | `TimeSource.now(fresh=True)`; `hx_win.lookup_group_sid` | `hx_win.current_identity` (at start-up), `monitors`, `play_alert`, `stop_alert` |
   | `LocalStore` calls, except `HxSettings.load` at start-up (storage.md §7.1 rule 5) | `HxSettings.load` in `make_services` |
   | `HxSettings.save`; `png_preview`; reading an original photo for `PhotoWindow`; opening an original file (`os.startfile`) | `photo_from_png`, `bmp_to_image` (Pillow on bytes already in memory), `PhotoImage`s of a picture already read |

3. Short blocking work goes through `services.bg.run(work, done)`: `work` runs on a worker thread and
   must not touch Tk; `done(result, error)` runs on the Tk thread and starts with
   `if not self.winfo_exists(): return`. `services.bg` is `stripes.Background` in the app and
   `SyncBackground` in tests.
4. Long runs (a Send, a Stop/Resume/Set clocks action, a test print) run on a named thread from
   `hx_ui_common.start_thread(services, name, ref, target)`: names `hx-send-<send_id>`,
   `hx-action-<action_id>` and `hx-test-<run_id>` (architecture §7.1). The wrapper is a daemon thread
   that calls `target()`. An `HxError` posts `WorkFailed(ref=ref, msg=e.msg)`; any other exception is
   printed with `traceback.print_exc()` and posts `WorkFailed(ref, Msg(N_("Stripes hit a problem:
   {error}"), {"error": f"{type(e).__name__}: {e}"}))`. Results arrive only as bus events
   (`SendEvent`, `SendFinished`, `ActionEvent`, `ActionFinished`, `TestEvent`, `TestFinished`,
   `WorkFailed`), posted by the runner with `on_event=services.bus.post`.
5. A window subscribes with `HxWindow.subscribe(event_type, callback)` and filters by its own
   `send_id`, `action_id` or `run_id`. `destroy()` unsubscribes every token and cancels every timer
   made with `later()`. `LineBoard` (a frame) does the same in its own `destroy()`.
6. Timers: the bus pump every 100 ms and `gc.collect()` every 2 s are started by `start_services`
   (§15.2). Screens add only these timers: the Send countdown (200 ms), the age and clock tick on cards
   and the floor board (1000 ms), the floor-board flash (`FLOOR_FLASH_MS`). Nothing in the UI polls the
   share or a printer on a timer; everything live comes from `BoardUpdate`, `DataUpdate`,
   `ShareHealth` and `PollerUpdate` events.
7. `STRICT_THREADS`: `Texts.set`, `Texts.msg`, `BagText.set`, `PictureView.set_bmp` and every bus
   callback registered through `HxWindow.subscribe` call `check_tk_thread()` first. Tests set
   `hx_ui_common.STRICT_THREADS = True`, so a UI update from a worker fails loudly.
8. Nothing starts a thread in a constructor (architecture §9.3 rule 3): windows start work from button
   handlers or from `root.after(0, ...)` scheduled in the constructor, never directly.
9. A window that started a long run marks it with `mark_running(key, what)` and calls `mark_done(key)`
   when the matching `*Finished` or `WorkFailed` event arrives, so `can_quit` knows (§15.4).

### 2.2 Text and language

1. Every text the UI shows goes through `tr()` with a literal first argument (so `test_i18n.py` finds
   it), or through `text(msg)` for a `Msg` from another module. Text built ahead of time is marked
   `N_()`. No f-string or concatenation is ever passed to `tr()`.
2. `HxWindow` and `MapleJetTab` set `hx_no_translate = True`, so `App._retranslate` never walks into
   MapleJet screens (architecture §10.4 rule 1). Each MapleJet window re-does its own text instead:
   fixed text is set through its `Texts` (`self.texts.set(widget, N_text, **values)`), and
   `retranslate()` re-applies it, re-shows `ChoiceBox`/`RecordBox` values, and calls
   `refresh_texts()`, which re-renders every text that came from a `Msg` (headlines, problems,
   progress). `hx_ui.language_changed(services)` calls `retranslate()` on the tab and on every
   `open_windows()` window. Nothing is closed and reopened, so a half-filled Send window keeps its
   choices.
3. Record names, codes, user and computer names, file names, free-text questions and every other value
   typed by an administrator are data: they are shown as they are, never passed to `tr()`, and never
   registered in `Texts`. Values inside a `Msg` (architecture §4) are rendered by `text()`.
4. A `ttk.Notebook` tab, `LabelFrame`, `Button`, `Checkbutton`, `Radiobutton` or `Label` may show
   translated text or data, never bag text (§2.3).
5. Choices with a fixed meaning (placement, cartridge, ink colour, film status, item kind, file kind,
   resolution, size mode) use `ChoiceBox` with `(key, N_ text)` pairs. Their keys are what is stored.
   A widget whose text joins data and a translated word (a print text's name with its translated
   status, a tree cell with an item name) isn't put in `Texts`; `refresh_texts()` renders it again.
6. `join_names(names)`: one name as it is; two as `tr("{first} and {last}", first=a, last=b)`; more as
   `", ".join(names[:-1])` for `first` and the last name for `last`.
7. Numbers are shown with `f"{x:g}"` (150.0 → "150", 152.5 → "152.5"); times with `when()` or
   `time.strftime`. Neither is translated. The Tcl `clock` command is never used (deploy.md §2.2: the
   exe leaves out Tcl's time-zone data).
8. Window titles are set through `Texts.title`. Tab texts of the two top tabs are names and aren't
   translated (architecture §10.2 rule 4).

### 2.3 Bag text

1. Bag text is anything that prints on a bag, including the origin line, the one-line bag text, test
   labels such as "P150 | B1", country names in an origin and the text before and after an item
   (architecture §2). It is English only and never passes through `tr()`.
2. Bag text is shown only in `BagText` and `PictureView` canvases, in `Entry` and `Text` contents, in
   `Combobox` values, `Listbox` items and `Treeview` cells (architecture §10.4 rule 2). It is never the
   `text` of a Label, Button, LabelFrame, Checkbutton, Radiobutton or Notebook tab, a Treeview heading
   or a window title, and never a value of a `Msg`.
3. `BagText` details:
   1. The canvas has `highlightthickness=0`, padding 2 px, and draws one `create_text` item at (2, 2),
      `anchor="nw"`, `fill=foreground`, `font=(family, size, "bold" if bold else "normal")`,
      `width=wrap_px or 0`.
   2. `set(text)` keeps `text` as given (no strip, no case change), redraws, and sets the canvas
      `height` to the text's bbox height + 4 and, when `wrap_px` is None, its `width` to the bbox width
      + 4. An empty text keeps the height of one line, so layouts don't jump.
   3. `get()` returns exactly what was last `set` (`""` before).
4. "(origin unknown)" (states §5.3) is a translated Label next to an empty `BagText`, never text inside
   it.
5. `PictureView` details: `set_bmp(None)` or `b""` draws a grey `#e5e7eb` box with a `RULE_FG`
   outline and no text. Otherwise `bmp_to_image(bmp).convert("L")`, resized to the canvas width keeping
   1024:160 (`Image.LANCZOS` when shrinking, `Image.NEAREST` when enlarging), shown through
   `ImageTk.PhotoImage(img, master=self)` kept on `self._photo`, with a 1 px `#9ca3af` border. A BMP
   Pillow can't read draws the grey box with a red outline; `set_bmp` never raises. It redraws on
   `<Configure>`.
6. Text before and after an item is shown in Treeview cells inside straight double quotes, so leading
   and trailing spaces are visible: `"Packed on "`.

### 2.4 Look

`setup_styles(root)` defines these `ttk` styles once (the existing `Print.TButton`, `Warn.TLabel`,
`Ok.TLabel`, `Bad.TLabel` and `Muted.TLabel` of `stripes.py` are reused, not redefined):

| Style | Settings | Used for |
|---|---|---|
| `Hx.Main.TButton` | font (UI_FONT, 11, "bold"), padding (16, 6) | the main button of a window or card |
| `Hx.Big.TButton` | font (UI_FONT, 14, "bold"), padding (24, 10) | Send, Everything is right, Try again, Leave it |
| `Hx.Head.TLabel` | font (UI_FONT, 15, "bold") | window headings |
| `Hx.Section.TLabel` | font (UI_FONT, 11, "bold") | section headings |
| `Hx.Small.TLabel` | font (UI_FONT, 8), foreground MUTED_FG | details, ages |
| `Hx.Card.TFrame`, `Hx.Card.TLabel` | background CARD_BG | inside cards |

Plain `tk` widgets are used where a colour must be set directly: card colour bars, the red windows,
the floor board, highlights. Fonts are `(UI_FONT, size, weight)` tuples; the floor board uses pixel
sizes (negative Tk sizes, §10.3). Colours for states are always `COLOUR_HEX[view.colour]`.

| Where | Bag text size (points, bold) |
|---|---|
| Send window: origin line of a line | 20 |
| Send window: one-line bag text, origin change | 11 |
| Result window: origin of a machine | 22 |
| Red windows: last confirmed origin | 24, white on red |
| Line card: machine origin | 13 |
| Resume dialog: origin | 20 |
| Countdown page: origin of each line | 16 |
| Floor board | pixel sizes from `floor_layout` |

### 2.5 Windows

1. Every MapleJet window is an `HxWindow` whose master is the `App` (`services.nav`'s root). They are
   ordinary `Toplevel`s: they show even while the main window is hidden in the tray.
2. `services.nav` keeps one window per `key`. Opening a window whose key is open calls `show()` on it
   instead (§15.3).
3. Default sizes (`geometry`) and minimum sizes:

   | Window | Size | Minimum |
   |---|---|---|
   | `SendWindow` | 1000x780 | 760x560 |
   | `ResultWindow` | 1000x780 | 760x560 |
   | `FailureWindow`, `StopDialog` in its red state | full screen (`attributes("-fullscreen", True)`, `-topmost` True) | |
   | `StopDialog`, `ResumeDialog`, `SetClocksDialog` | 640x420 (Resume 900x640) | 480x320 |
   | `MonitorWindow` | 1180x780 | 760x480 |
   | `FloorBoard` | §10.2 | |
   | `AdminWindow` | 1180x820 | 900x600 |
   | `TestPrintWindow`, `CalibrationWindow` | 1000x760 | 760x560 |
   | `AlertPopup` | 520x320 | 420x240 |
   | `UserSettingsDialog`, `AdjustmentDialog` | 480x320 / 480x300 | |
   | `PhotoWindow` | sized to the photo, at most 90% of the screen (§5.11) | 320x240 |

4. Windows open centred on the main window when it is visible, else on the primary screen.
5. A window that is running something refuses a plain close: its `close()` explains or asks (each
   window says how).

### 2.6 Dialogs, problems and errors

1. `tell` and `ask` are the only modal dialogs (tests patch them). Every UI module calls them through
   the module (`hx_ui_common.tell(...)`), never through `from hx_ui_common import tell`, so one patch
   covers every screen. `tell(parent, msg, kind)` calls
   `messagebox.showinfo`/`showwarning`/`showerror(APP_TITLE, text(msg), parent=parent)`. `ask` opens a
   small modal `Toplevel` (title APP_TITLE, `text(msg)` wrapped at 420 px, two buttons `tr(yes)` and
   `tr(no)`, the "yes" button on the right in `Hx.Main.TButton`), `grab_set()`, `wait_window()`;
   Escape and the close box mean "no". It returns True for "yes". The dialog's class sets
   `hx_window = True`, so the Zebra shortcuts ignore it too.
2. An `HxError` from a worker is shown with `text(e.msg)`: in the window's problem area when the
   window has one, otherwise with `tell(..., kind="error")`. Any other exception is shown as "Stripes
   hit a problem: {error}" and printed with its traceback.
3. Validation problems (`list[Msg]`) are shown as a list under the form, one per line, in
   `Bad.TLabel`, each `text(msg)`, and block the action they belong to.
4. Warnings that don't block (origin conflicts, film-stop warnings, "print length unknown") use
   `Warn.TLabel`.

### 2.7 Role gating

1. Identity comes from the Windows sign-in: `hx_win.current_identity()` reads the enabled group SIDs
   from the process token through ctypes (architecture §6.3) once, in `make_services`. The roles are
   checked against the group SIDs in the company settings, so they work offline:
   `roles(services)` returns `Roles(sender=identity.is_sender(company), admin=identity.is_admin(company),
   acknowledge=hx_state.can_acknowledge(identity, company))` with `company =
   services.data.current.company`, or `Roles()` (all False) while `data.current` is None.
2. Roles are re-evaluated on every `DataUpdate` (an administrator may change the SIDs). Group changes
   in Active Directory take effect at the person's next Windows sign-in, because the token is read
   once.
3. What each role gets. A widget that a role doesn't allow is not shown (`grid_remove()`/
   `pack_forget()`), not merely disabled. Buttons that a state doesn't allow are disabled on windows
   and line cards, and left out of machine rows, which stay short (§9.1):

   | Feature | Everyone | Senders | Administrators |
   |---|---|---|---|
   | MapleJet tab, line cards, MapleJet monitor, floor board, My settings | yes | yes | yes |
   | Send..., Set all clocks..., Set clocks... (line), Stop..., Resume..., Try again, Leave it, Fix and send again | | yes | |
   | Everything is right / Something's wrong, Confirm... | | only the person who sent it (`same_user`) | |
   | Acknowledge | | yes | yes |
   | Admin..., Test print..., Calibrate..., Set up... (on a "No printer set up" card) | | | yes |
   | Administrator strip on the tab: `DataService.problems` and alerts with the `"admins"` recipient | | | yes |

4. The UI gating is a convenience. The real checks are elsewhere and stay: `merge_plan` raises
   `NotAllowed` for non-senders, `confirm` for anyone but the sender, `DataService.save` for
   non-administrators, and the share's permissions enforce everything (storage.md §14).
5. A `Navigator.open_*` call that the user's roles don't allow shows `tell` with "Only Stripes
   MapleJet Senders can do this." or "Only Stripes Administrators can do this." and opens nothing.
6. The tab says who is signed in and what they can do (§4.2), so a person missing a button knows why.

## 3. The two tabs

### 3.1 The split

`App._build` follows architecture §10.2 exactly. What moves:

```
App window
┌─────────────────────────────────────────────────────────────────────────────────┐
│ [logo] Stripes                                     English | Español             │  header: stays in `outer`
│        Zebra labels and MapleJet bag printing                  Version 2.0.0     │
│ ─────────────────────────────────────────────────────────────────────────────── │  separator: stays
│ [  Zebra  ][  MapleJet  ]                                                        │  self.top_tabs
│ ┌ Zebra tab (self.zebra_tab) ───────────────────────────────────────────────────┐│
│ │ Printer: [ZT410 v] [Check printer] [Manage printers...] [Printer monitor] ... ││  printer row
│ │ Label stock: [2 x 1 in v]  ZT410 via IP ...                                    ││  stock row
│ │ ┌[ Simple ][ Advanced ][ ZPL ]──────────┐ ┌ Preview ────────────────────────┐ ││  body: inner notebook
│ │ │ ...                                    │ │ ...                             │ ││        and preview
│ │ │ Recent: [............ v] [New label]   │ │                                 │ ││  recent row (in left)
│ │ └────────────────────────────────────────┘ └─────────────────────────────────┘ ││
│ │ Ready. Tip: Ctrl+P prints.                                                     ││  status (before=bottom)
│ │ Copies: [1] [Exact preview (online)]                          [   Print   ]    ││  bottom (before=body)
│ └────────────────────────────────────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────────────────────────────────┘
```

1. Under the Zebra tab, unchanged and in the same packing order: the printer row (`top`), the stock row
   (`stock_row`), `body` (the inner Simple/Advanced/ZPL notebook with the recent row, and the Preview
   pane), `bottom` (Copies, Exact preview, Print) and `status`. `bottom` keeps `side="bottom",
   before=body` and `status` keeps `side="bottom", before=bottom`, now inside `self.zebra_tab`.
2. Above the tabs, in `outer`: the header and its separator only.
3. The Zebra tab is selected when Stripes starts, and the Simple box keeps the focus, as today.
4. `App.minsize(900, 640)` is unchanged. The MapleJet tab must work at that size: its line board
   scrolls.

### 3.2 Shortcuts

1. Ctrl+P and Ctrl+Enter go through `App._zebra_shortcut` (architecture §10.3): they print a Zebra
   label only when the Zebra tab is selected and the key press didn't come from an `HxWindow`.
2. The Simple box's own `<Control-Return>` binding is inside the Zebra tab and stays.
3. MapleJet screens define no global shortcuts. Inside MapleJet windows: Return activates the focused
   button only (Tk default); Escape closes `UserSettingsDialog`, `PhotoWindow`, `AdjustmentDialog` and
   the `ask` dialog, and nothing else. In particular no key sends, confirms, stops or closes the red
   window.

### 3.3 Two monitors

| | Zebra | MapleJet |
|---|---|---|
| Name (English / Spanish) | "Printer monitor" / "Monitor de impresoras" (unchanged) | "MapleJet monitor" / "Monitor MapleJet" |
| Class | `stripes.PrinterMonitor` (unchanged) | `hx_ui_monitor.MonitorWindow` |
| Opened from | the Zebra tab's printer row; tray "Printer monitor" | the MapleJet tab's button; tray "MapleJet monitor" (architecture §10.6) |
| On a language switch | closed and reopened by `stripes.py` (as today) | retranslated in place |

The floor board is the MapleJet monitor in floor-board mode (`FloorBoard`), opened from the MapleJet
tab, from the MapleJet monitor's "Floor board" button, or at startup (§14).

### 3.4 Sends and the poller are outside the tabs

`hx_ui.start_services` runs before `_build` (architecture §10.1). Switching tabs only calls
`MapleJetTab.on_shown()`/`on_hidden()`, which change what is drawn and the monitor flag (§15.3); no
Send, action, test, poll or alert sound is ever stopped by a tab switch, by hiding to the tray or by
closing a MapleJet window other than through its own buttons.

## 4. The MapleJet tab

### 4.1 Layout

```
┌ MapleJet tab ──────────────────────────────────────────────────────────────────────────┐
│ [Send...] [Set all clocks...]          [MapleJet monitor] [Floor board] [Admin...] [My settings...] │  row 0
│ Signed in as EXAMPLE\jsmith. You can send and acknowledge alerts.                      │  row 1: who_label
│ ┌ banner (only when board_banner() or share down) ──────────────────────────────────┐  │  row 2: banner_frame
│ │ Server unreachable: showing this PC's own checks · next check in 18 s [Refresh now]│  │
│ └────────────────────────────────────────────────────────────────────────────────────┘  │
│ ┌ For administrators (only when there is something) ────────────────────────────────┐  │  row 3: admin_frame
│ │ ! MapleJet isn't set up on the server yet.                                          │  │
│ │ ! Something else is talking to printer Printer 1                                    │  │
│ └────────────────────────────────────────────────────────────────────────────────────┘  │
│ ┌ LineBoard (scrolls) ───────────────────────────────────────────────────────────────┐  │  row 4: board (weight 1)
│ │ [Line 1 card]                         [Line 2 card]                                 │  │
│ │ [Line 3 card]                                                                        │  │
│ └────────────────────────────────────────────────────────────────────────────────────┘  │
│ This PC is checking 2 printers.                                                         │  row 5: footer_label
└─────────────────────────────────────────────────────────────────────────────────────────┘
```

| Widget (attribute) | Shown to | Does |
|---|---|---|
| `send_button` "Send..." (`Hx.Main.TButton`) | senders | `nav.open_send()` |
| `clocks_button` "Set all clocks..." | senders | `nav.set_clocks(<every active line id>)` |
| `monitor_button` "MapleJet monitor" | everyone | `nav.open_monitor()` |
| `floor_button` "Floor board" | everyone | `nav.open_floor_board()` |
| `admin_button` "Admin..." | administrators | `nav.open_admin()` |
| `settings_button` "My settings..." | everyone | opens `UserSettingsDialog` |
| `who_label` | everyone | §4.2 |
| `banner_frame` with `banner_label` (`Warn.TLabel`) and `refresh_button` "Refresh now" | everyone, when there is a banner | §4.3 |
| `admin_frame` with one `Bad.TLabel` per line | administrators, when non-empty | §4.4 |
| `board` (`LineBoard`) | everyone | §9.1 |
| `footer_label` (`Muted.TLabel`) | everyone | §4.5 |

### 4.2 Who is signed in

`who_label` = "Signed in as {user}." followed by one sentence by roles:

| Roles | Sentence |
|---|---|
| sender and admin | "You can send, change settings, run test prints and acknowledge alerts." |
| sender | "You can send and acknowledge alerts." |
| admin | "You can change settings, run test prints and acknowledge alerts." |
| neither | "You can watch the lines. To send, ask IT to add you to {group}." (`group` = `company.sender_group`) |
| data not loaded | "Loading the MapleJet settings..." |

The two parts are separate `Texts` entries in one label: `texts.msg(who_label, Msg(N_("{signed_in}
{can}"), {"signed_in": Msg(N_("Signed in as {user}."), {"user": identity.user}), "can": Msg(...)}))`.

### 4.3 Banner

1. `board_banner(board)` (states §5.7) when it isn't None, shown with `text()`. In standalone mode the
   `refresh_button` "Refresh now" shows: it calls `poller.refresh_now()` and, when that returns False,
   shows "You can refresh once every {seconds} s." (`seconds` = `int(timings.refresh_now_min_s)`) in
   the banner for 3 s.
2. No banner while the board is fine. The banner counts down between updates by subtracting the seconds
   since the `BoardUpdate` arrived from `next_check_s` (never below 0).

### 4.4 Administrator strip

For administrators only, one line each, in this order: every `DataService.problems` message (storage.md
§13.7), then every active alert on the board whose `recipients` include `"admins"`, as "{machine}:
{alert}" (`machine` = the machine's name, `alert` = the alert's `Msg`). Nothing shown when both are
empty. Updated on `DataUpdate` and `BoardUpdate`.

### 4.5 Footer

From the last `PollerUpdate` (subscribe; initial value `poller.last_update()`):

| `mode` | Text |
|---|---|
| `"share"`, polling n > 0 | "This PC is checking {n} printers." ("This PC is checking 1 printer." for 1) |
| `"share"`, none | "This PC isn't checking any printers." |
| `"standalone"` | "This PC is checking {n} printers on its own while the server can't be reached." |
| `"idle"` | "This PC isn't checking any printers." |

### 4.6 Empty and not-set-up states

| Situation | Board area shows |
|---|---|
| `data.current` is None and `data.problems` non-empty | the problems, one per line (`Warn.TLabel`), to everyone |
| `data.current` is None, no problems | "Loading the MapleJet settings..." |
| no active lines | "No lines are set up yet." and, for administrators, "Open Admin... to add printers, lines and machines." |

### 4.7 on_shown / on_hidden

`on_shown()` sets `nav.tab_shown = True`, calls `nav.update_monitor_flag()`, shows
`watcher.latest` on the board when there is one and starts the 1 s age tick. `on_hidden()` sets it
False, calls `update_monitor_flag()` and stops the tick. The board keeps receiving `BoardUpdate`s
while hidden (cheap), so it is current when shown.

## 5. The Send window

### 5.1 Opening

1. `nav.open_send(prefill=None, line_ids=None)`: senders only (§2.7 rule 5). With no Send window open,
   it opens `SendWindow(root, services, prefill=prefill, line_ids=line_ids)`.
2. With one open on its choice page: with no arguments it is shown. With `prefill` or `line_ids`, if
   the open window has any product chosen, `ask(Msg(N_("Replace the choices in the Send window?")),
   yes=N_("Replace them"), no=N_("Keep them"))`; then `fill(prefill)` or `tick_lines(line_ids)`.
3. With one open on its countdown or working page: it is shown, and `tell(Msg(N_("A Send is running.
   Wait for it to finish.")))`.
4. The window subscribes to `DataUpdate` and `ShareHealth` (offers and banners, §5.2), `BoardUpdate`
   (machine states and current origins), `SendEvent`, `SendFinished` and `WorkFailed`. In its
   constructor it schedules `after(0)` a `bg.run` of `data.pending_logs()` for the queue line.

### 5.2 Layout

```
┌ MapleJet Send ──────────────────────────────────────────────────────────────────────────────┐
│ Send to the bag printers                                    Sending as EXAMPLE\jsmith on FLOOR01│  heading_label, as_label
│ ┌ local_banner (amber; only from the local copy) ────────────────────────────────────────┐ │
│ │ Using this PC's copy of the templates, as of 2026-10-29 13:40. The server can't be     │ │
│ │ reached, so every machine's result will be amber.                                      │ │
│ └────────────────────────────────────────────────────────────────────────────────────────┘ │
│ ┌ blocked_banner (red; only when can_send() is False) ───────────────────────────────────┐ │
│ │ Can't reach the server since 2026-10-28 09:12.                               [Retry]   │ │
│ └────────────────────────────────────────────────────────────────────────────────────────┘ │
│ 2 log entries waiting to upload.                                                  queue_label│
│ ! problems_label: DataService.problems, one per line (Warn)                                 │
│ Lines:  [x] Line 1   [x] Line 2   [ ] Line 3                                    line_checks │
│ ┌ Scroller (blocks, in line order) ──────────────────────────────────────────────────────┐ │
│ │ LineBlock(Line 1)  (§5.3)                                                               │ │
│ │ LineBlock(Line 2)                                                                       │ │
│ └────────────────────────────────────────────────────────────────────────────────────────┘ │
│ [Close]                                                       [ Send to 3 machines ]       │  close_button, send_button
└─────────────────────────────────────────────────────────────────────────────────────────────┘
```

1. `local_banner` (`tk.Frame`, background `#fef3c7`, text `#92400e`) shows when
   `data.current.source == "local"`: "Using this PC's copy of the templates, as of {time}. The server
   can't be reached, so every machine's result will be amber." (`time` = `when(data.current.reached_at)`).
   This is the plan's "templates as of <time>".
2. `blocked_banner` (background `#fee2e2`, text `#991b1b`) shows when `data.can_send()` returns
   `(False, msg)`: `text(msg)` (e.g. "Can't reach the server since {time}.") and `retry_button` "Retry",
   which calls `services.store.probe_now()` and `services.data.reload()`; the banner updates on the next
   `ShareHealth`/`DataUpdate`. While it shows, `send_button` is disabled.
3. `queue_label` (`Muted.TLabel`): "{n} log entries waiting to upload." ("1 log entry waiting to
   upload." for 1); hidden at 0. Refreshed after each `DataUpdate` and after each Send.
4. `problems_label`: `data.problems`, one per line, hidden when empty.
5. `line_checks`: a `ttk.Checkbutton` per active line, sorted by `hx_state.natural_key(name)`, text =
   the line's name (data), variable `line_vars[line_id]`. Ticking adds that line's `LineBlock` to the
   scroller (blocks stay in line order); unticking removes it but keeps its `choice()` in
   `self.kept[line_id]`, and re-ticking restores it with `fill()`.
6. `as_label`: "Sending as {user} on {computer}" (`Muted.TLabel`).
7. `close_button` "Close": with any product chosen, `ask(Msg(N_("Close the Send window? Your choices will
   be lost.")), yes=N_("Close"), no=N_("Keep it open"))`.

### 5.3 A line block: lines, then product, then film, then print text

Each ticked line gets one `LineBlock`, a `ttk.LabelFrame` whose text is the line's name. Rows appear in
the plan's order; a row is shown only when the one before it has a value.

```
┌ Line 1 ────────────────────────────────────────────────────────────────────────────────────┐
│ 1 Machines    [x] Line 1 Bagger 1  Printing             [x] Line 1 Bagger 2  Printer off   │  machine_vars
│ 2 Product     [Example Co 2lb Onion                         v]                              │  product_box
│ 3 Film                                                                                      │
│   ┏━━━━━━━━━━━━━━━━━━━━━━━┓ ┌───────────────────────┐ ┌───────────────────────┐             │  film_cards (the chosen
│   ┃(•) EXCO1 · Rev C      ┃ │( ) EXCO2 · Rev D      │ │( ) EXCO3 · Rev A      │             │  one has a blue border)
│   ┃Code on the back of the┃ │Code on the front of   │ │Code on the back of the│             │  film_var, film_radios
│   ┃bag                    ┃ │the bag                │ │bag                    │             │
│   ┃[front    ] [back     ]┃ │[front    ]  No photo  │ │ No photo    No photo  │             │  thumbs, each in a
│   ┃[100x150  ] [100x150  ]┃ │[100x150  ]            │ │                       │             │  100x150 box
│   ┗━━━━━━━━━━━━━━━━━━━━━━━┛ └───────────────────────┘ └───────────────────────┘             │
│ 4 Print text  [Packed on + origin                           v]                              │  print_text_box
│ 5 Lot         [4417      ]  (4 to 8 digits)                                                 │  lot_entry
│   Countries, in the order they print:                                                       │
│     ┌──────────┐ [Up] [Down] [Remove]       [Mexico       v] [Add]                          │  country_list, country_box
│     │USA       │                                                                            │
│     │Canada    │                                                                            │
│     └──────────┘                                                                            │
│   Best before code [________]  (at most 10 characters)                                      │  free_entries (label = item.label)
│ ─────────────────────────────────────────────────────────────────────────────────────────── │
│ Origin on the bag:                                                                          │
│   {Product of USA and Canada}                                       BagText 20 bold         │  origin_text
│   Line 1 Bagger 1   {USA → USA and Canada} (highlighted)                                    │  change_texts[mid]
│                     Prints: {Packed on 10/29/26 / Product of USA and CanadaLot 4417}        │  bag_texts[mid]
│                     Print position 150 mm   (150 - 412 + 0) mod 610 = 348 mm                │  position_labels[mid],
│                                             (administrators only)                           │  working_labels[mid]
│                     ! The print would still be running when the film stops ...              │  warning_labels[mid]
│   Line 1 Bagger 2   Same origin as now.                                                     │
│                     Prints: {...}                                                           │
│ Still needed: Enter the lot.                                                                │  problems_label
└─────────────────────────────────────────────────────────────────────────────────────────────┘
```

1. **Machines.** A `ttk.Checkbutton` per machine of `data.line_machines(line_id)` (text = machine name),
   then a label with `text(view.headline)` in `COLOUR_HEX[view.colour]` from the latest board. All are
   ticked by default, except a machine whose view code is `not_set_up` (unticked and disabled). The
   sender unticks a machine that's down (plan).
2. **Product.** `product_box` (`RecordBox`): the active products that have at least one film in
   `hx_message.films_for_line(line, product_id, data)`, sorted by `natural_key(name)`, shown by name.
   None at all: the row shows "No films are ready for this line's cartridge type." instead. Choosing a
   product clears the film and the print text.
3. **Film.** The heading "Film" on its own row, then one card per film of `films_for_line(line,
   product_id, data)`, ordered as the admin list orders them, by code, then artwork revision
   (`sorted_films`). The cards (`film_cards[film_id]`, a `tk.Frame` with a 2 px border, `COLOUR_HEX[BLUE]`
   for the chosen film and `DIM_FG` for the others) sit in a grid that takes the block's whole width
   (indented 20 px) and wraps: as many cards to a row as fit, all as wide as each other, so four films
   sit side by side in the 1000x780 window and the print text, lot, countries and origin stay in view.
   Each card holds:
   - a `ttk.Radiobutton` with variable `film_var` (value = film id, initially `""`: no film is
     pre-selected, plan), text `f"{film.code} · {film.artwork_revision}"` (data; just the code when the
     revision is empty);
   - "Code on the back of the bag" / "Code on the front of the bag" (`film.side`, `Muted.TLabel`, wrapped
     to the card's width). Clicking the card or this text picks the film too;
   - the front and back photo thumbnails side by side: the film's last `FileRef` of kind `front_photo` and
     of kind `back_photo`. Each is a `ttk.Label` centred in a fixed `THUMB_BOX` (100x150) frame, with a
     `PhotoImage` made by `photo_from_png(..., box=THUMB_BOX)` from `data.preview_bytes(film_id, sha256)`
     (one `bg.run` per photo, started when the cards are built): a tall bag photo is 150 px tall, and
     every photo keeps its shape. Missing or unreadable: "No photo" (`Muted.TLabel`) in the same box. A
     load that finishes after the cards were built again is ignored. Clicking a thumbnail calls
     `show_photo` with the same PNG, the photo's `FileRef` as `original` and the caption
     `f"{film.code} · {film.artwork_revision}: "` + the translated kind (§16.1 "Front photo"/"Back
     photo"); a second click shows the window already open (§5.11).
   Choosing a film sets the print text to `film.default_print_text_id` when it is in the offered list
   (plan: "the print text defaults to the film's"), else to none, and clears the countries when the
   film is pre-printed.
4. **Print text.** `print_text_box` (`RecordBox`): `hx_message.print_texts_for_film(film, data)`, by
   name.
5. **What the sender fills in**, shown once a print text is chosen, each only when the print text has
   that kind of item:
   - **Lot** (a `lot` item): `lot_entry` (`ttk.Entry`, width 12) and the hint "({min} to {max} digits)"
     from `company.lot_min_digits`/`lot_max_digits`.
   - **Countries** (an `origin` item and a film that isn't pre-printed): `country_list` (`tk.Listbox`,
     height 4) holds the chosen `Country.name`s in the order picked, which is the order they print
     (plan); `country_box` (`ttk.Combobox`, read-only) offers the active countries not yet chosen, in
     the company's order; buttons `add_country_button` "Add", "Move up", "Move down" and "Remove" act on it.
     `countries` (list) mirrors the listbox.
   - **Pre-printed origin** (a pre-printed film): "Origin is pre-printed on this film:" and a `BagText`
     (size 13, bold) with `origin_preview(film, [])`, e.g. "USA (pre-printed)". No country row.
   - **Free text**: for each `free_text` item, in item order, a `ttk.Label` with `item.label` (data),
     an entry `free_entries[item.id]` (width `min(40, item.max_len + 2)`), the hint "(at most {n}
     characters)" and, when `item.required` is False, "(optional)". Entry contents are bag text.
6. **Origin and preview** (§5.4), then `problems_label` (§5.5).
7. Every change (variable traces, `<<ComboboxSelected>>`, `<KeyRelease>` in entries debounced by
   300 ms with `later()`) recomputes the block: `problems()`, then the trial plan (§5.4), then the
   window's `send_button` state.

### 5.4 The origin in large type, highlighted changes and the preview

1. `origin_text` (`BagText`, size 20, bold, `wrap_px` 760) shows `origin_preview(film, countries)` as
   soon as a film is chosen (empty until countries are chosen). It is the exact origin line that will
   print, or the pre-printed text the log and floor board will show.
2. When `problems()` is empty, the block makes a **trial plan**: `merge_plan(SendRequest(send_id=
   "sd_preview", sender=identity.user, computer=identity.computer, created_at=services.clock.time(),
   lines=[choice]), data, identity)` on the Tk thread (pure). Its `MachinePlan`s fill one row per
   ticked machine:

   | Widget | Content |
   |---|---|
   | machine name label | `plan.machine.name` |
   | `change_texts[mid]` (`BagText` 11 bold, background `HIGHLIGHT_BG`) | `origin_change_text(view.origin_line, plan.origin_line)` when it isn't `""` and `view.origin_line` isn't `""` (plan: "USA → USA and Canada") |
   | instead, a `Muted.TLabel` | "Same origin as now." when the change text is `""`; "(origin unknown) →" followed by a highlighted `BagText` with the new origin when `view.origin_line` is `""` |
   | `bag_texts[mid]` (`BagText` 11, `wrap_px` 620) after "Prints:" | `plan.bag_text` |
   | `position_labels[mid]` (`Hx.Small.TLabel`) | `text(placement_text(plan.position))`: "Delay 480 ms" on a delay machine (the machine's adjustment included), "Print position 150 mm" on an encoder one. Senders don't need the arithmetic |
   | `working_labels[mid]` (`Hx.Small.TLabel`, after it; administrators only, `roles(services).admin`) | `plan.position.text`, the English working (data): "480 + 0 = 480 ms = 480000 µs", "(150 - 412 + 0) mod 610 = 348 mm" |
   | `warning_labels[mid]` (`Warn.TLabel`, one per warning) | `text(w)` for each `plan.warnings` (film-stop warning, "Print length unknown: ...") |

   `view` is that machine's `MachineView` in `watcher.latest` (None: treated as origin `""`). A
   `PlanError` from the trial shows its `problems` in `problems_label` instead of the rows.
3. The changes are recomputed on every `BoardUpdate`, because what a machine prints now can change while
   the window is open.
4. **Unticked machines still printing.** For each machine of the line that isn't ticked, with `view` its
   `MachineView` (None when the board has none): when (`view` is None, or `view.code != "off"` and
   `view.printing is not False`) and (`view` is None, or `view.origin_line == ""`, or
   `origin_change_text(view.origin_line, <the block's origin line>) != ""`), the block shows a warning
   row in `HIGHLIGHT_BG`: a `Warn.TLabel` "{machine} isn't ticked and is still printing:", a `BagText`
   (11, bold) with `view.origin_line` (or the "(origin unknown)" label), and "This line will print two
   origins." Below the warning rows, `two_origins_var` (`ttk.Checkbutton`, "I know this line will print
   two origins") must be ticked before `send_button` is enabled (§5.5); it is unticked again whenever
   the set of warned machines changes. A line whose unticked machines are all off, or print the same
   origin, shows no warning. The plan lets the sender untick a machine that's down; this makes sure a
   wrong origin never keeps printing without someone saying so, and the send log records what the
   unticked machines were printing (send.md §17.2 `unticked`).

### 5.5 Checks and the Send button

1. `LineBlock.problems()` is `hx_message.validate_choice(self.choice(), data, identity)`. While it isn't
   empty, `problems_label` shows "Still needed:" and the messages, one per line (`Warn.TLabel`).
2. `send_button` (`Hx.Big.TButton`) reads "Send to {n} machines" ("Send to 1 machine"), `n` = ticked
   machines over all blocks. It is enabled only when: at least one block exists; every block's
   `problems()` is empty and its trial plan succeeded; every block that shows unticked-machine warnings
   (§5.4 item 4) has `two_origins_var` ticked; `data.can_send()[0]` is True; `roles.sender`; the
   window is on its choice page.
3. Pressing it:
   1. `data = services.data.current`; `request = self.request()`: `SendRequest(send_id=new_id("sd"),
      sender=identity.user, computer=identity.computer, created_at=clock.time(), lines=[block.choice()
      for each block in line order])`.
   2. `plan = merge_plan(request, data, identity)` on the Tk thread. `PlanError`: its problems go to the
      window's `problems_label` and nothing else happens. `NotAllowed`: `tell(e.msg, kind="error")`.
   3. `self.start(plan)`.

### 5.6 Choice values

`LineBlock.choice()` returns `LineChoice(line_id, machine_ids=[ticked, in machine order], product_id=
product_box.record_id(), film_id=film_var.get(), print_text_id=print_text_box.record_id(),
lot=lot_entry.get() (or "" without a lot row), countries=list(self.countries) ([] for a pre-printed
film), free_text={item_id: entry.get()})`. Values are passed as typed; `hx_message` strips them
(send.md §3.3).

### 5.7 Countdown, "Hang on", progress

`start(plan)`:

1. `self.plan = plan`; `page = "countdown"`; the choice page is hidden (`pack_forget`) and the progress
   page shown; `cancel_token = CancelToken()`.
2. `mark_running(f"send:{send_id}", Msg(N_("A Send to {machines} is still running."), {"machines":
   join_names(<machine names>)}))`.
3. `start_thread(services, f"hx-send-{plan.send_id}", plan.send_id, lambda: services.pipeline().run(plan,
   services.bus.post, cancel=cancel_token))`.

```
┌ MapleJet Send ───────────────────────────────────────────────────────────────┐
│ Sending to Line 1 and Line 2                                    (Hx.Head)    │  progress_heading
│ Line 1  {Product of USA and Canada}                             BagText 16   │  countdown_origins[line_id]
│ Line 2  {USA (pre-printed)}                                                  │
│                              Sending in                                      │  countdown_caption
│                                  4                    (UI_FONT, 48, bold)    │  countdown_label
│                             [  Cancel  ]                                     │  cancel_button (Hx.Big)
│ ──────────────────────────────────────────────────────────────────────────── │
│ Line 1 Bagger 1   …  Taking the printer's lock                               │  progress_rows[mid]
│ Line 1 Bagger 2   ✗  Line 2 is being sent by EXAMPLE\akim                     │
└──────────────────────────────────────────────────────────────────────────────┘
then, from the first printer request:
│ Hang on, we're working on it                          (UI_FONT, 18, bold)    │  working_label
│ Line 1 Bagger 1   …  Checking the new message in slot 9     {Picture 256}    │
│ Line 1 Bagger 2   ✓  Live: checked twice                    {Picture 256}    │
```

4. **Countdown.** The countdown is the pipeline's (architecture §6.7): the UI only shows it. Until the
   `SendEvent(step=STEP_LOCK, status="running")` arrives, `countdown_caption` reads "Getting ready..."
   and `countdown_label` shows `int(timings.countdown_s)`. From that event, a 200 ms timer shows
   `max(0, math.ceil(timings.countdown_s - (services.clock.time() - event.at)))` under "Sending in".
5. **Cancel** (`cancel_button`): `cancel_token.cancel()`, the button is disabled and the caption reads
   "Cancelling...". The window switches to the working page (item 7) and removes the button for good
   on `SendEvent(machine_id="", step=STEP_LOCK, status="ok")`, the pipeline's "go" (send.md §10.3 step
   8), after which the pipeline ignores the token (architecture §7.5 rule 2). As fallbacks, it also
   does so when the shown count reaches 0 or when any `SendEvent` arrives whose step is at or after
   `STEP_CHECK_MACHINE` in `STEPS`, whichever comes first.
6. `progress_heading` reads "Sending to {lines}" (`lines` = `join_names` of the plan's line names),
   and `countdown_origins[line_id]` (`BagText` 16 bold) shows each line's origin line from the plan.
7. **Working.** At that same moment (the "go" event, or a fallback of item 5) `page = "working"`: the
   countdown widgets are hidden and `working_label` reads "Hang on, we're working on it" (plan).
8. **Progress rows.** One per machine of the plan: name, a mark, `text(STEP_TEXTS[event.step])`, and
   `text(event.msg)` when the event has one; a `PictureView` (width 256) appears with the last
   `event.design.preview_bmp`. Whole-send events (`machine_id == ""`) show in `send_status_label`
   above the rows.

   | `SendEvent.status` | Mark | Style |
   |---|---|---|
   | `running` | … | `Muted.TLabel` |
   | `ok` | ✓ | `Ok.TLabel` |
   | `warning` | ! | `Warn.TLabel` |
   | `failed` | ✗ | `Bad.TLabel` |
   | `skipped` | – | `Muted.TLabel` |

9. `close()` on the countdown page: `tell(Msg(N_("Press Cancel to stop the Send.")))`; on the working
   page: `tell(Msg(N_("The Send is running. Wait for it to finish.")))`. The window stays.

### 5.8 When the Send finishes

On `SendFinished` with `result.send_id == plan.send_id`:

1. `mark_done(f"send:{send_id}")`.
2. `hx_send.is_cancelled(result)`: back to the choice page with every choice kept, `send_status_label`
   on the choice page reads `text(hx_message.NOTE_TEXTS["cancelled"])` ("Cancelled. Nothing was
   sent.", `Ok.TLabel`); `page = "choose"`.
3. Otherwise:
   1. If any machine has `ok`: `w = ResultWindow(root, services, result)`; `w.request = plan.request`
      (§6).
   2. If any machine isn't `ok`: `nav.open_failure(result, plan)` (§7), which comes up full screen on
      top.
   3. If any machine has the note `no_lock`: `nav.note_offline_send(result.send_id)` (§15.3).
   4. `self.destroy()`.

On `WorkFailed` with `ref == plan.send_id`: `mark_done`; on the countdown page (nothing sent: the
pipeline raised `PlanError` or `NotAllowed` before any printer request) the window returns to the
choice page and shows `text(msg)` in `problems_label`; on the working page it shows "Stripes hit a
problem during the Send. Check each machine's card on the MapleJet tab." and `text(msg)` in
`Bad.TLabel`, and `close()` is allowed again.

### 5.9 Prefill and Try again

1. `fill(request)`: for each `LineChoice` whose line is active, tick the line and `block.fill(choice)`,
   which sets, in order, the ticked machines, product, film, print text, lot, countries and free text.
   A value that is no longer offered is left empty, so `problems()` shows what is missing. A line that
   isn't active any more is skipped and noted in `problems_label`: "{line} isn't active any more."
2. `tick_lines(line_ids)` ticks those lines (choices empty).
3. `run_send(master, services, plan)` (Try again, §7.4) goes through the Navigator's one Send window
   (§2.5), never builds a second one:
   - no Send window open: `w = SendWindow(master, services, prefill=plan.request)`, then `w.start(plan)`;
     returns `w`;
   - one open on its choice page: `ask(Msg(N_("Replace the choices in the Send window?")),
     yes=N_("Replace them"), no=N_("Keep them"))`; yes: `w.fill(plan.request)`, then `w.start(plan)`,
     returns `w`; no: returns None;
   - one on its countdown or working page: `tell(Msg(N_("A Send is running. Wait for it to finish.")))`
     and returns None.

### 5.10 Test attributes

`SendWindow`: `page` ("choose" | "countdown" | "working"), `line_vars`, `blocks: dict[str, LineBlock]`,
`kept`, `local_banner`, `blocked_banner`, `retry_button`, `queue_label`, `problems_label`,
`send_button`, `close_button`, `plan`, `cancel_token`, `countdown_label`, `countdown_caption`,
`cancel_button`, `working_label`, `send_status_label`, `progress_rows: dict[str, ProgressRow]` (each
with `mark_label`, `step_label`, `msg_label`, `picture`).

`LineBlock`: `line`, `machine_vars`, `product_box`, `film_var`, `film_radios: dict[str,
ttk.Radiobutton]`, `film_cards: dict[str, tk.Frame]` (film id, in the order shown), `thumbs:
dict[tuple[str, str], ttk.Label]` (film id, kind), `print_text_box`, `lot_entry`, `country_list`,
`country_box`, `add_country_button`, `countries`, `free_entries`, `origin_text`, `change_texts`,
`bag_texts`, `position_labels`, `working_labels` (administrators only), `warning_labels`,
`problems_label`, `unticked_warnings: dict[str, tk.Frame]` (machine id), `two_origins_var`, `trial:
SendPlan | None`.

### 5.11 PhotoWindow: a film photo, large

`PhotoWindow(master, services, png=..., caption=..., original=FileRef | None)` shows a film photo so the
sender can tell films apart and read the small print (a nutrition panel, a barcode). The caption is data
and is also the window title.

```
┌ EXCO1 · Rev C: Front photo ─────────────┐
│ EXCO1 · Rev C: Front photo  (Hx.Section) │  caption_label
│ ┌──────────────────────────────────────┐ │
│ │ {the photo, fitted to the window}    │ │  canvas (scroll bars at full size only)
│ └──────────────────────────────────────┘ │
│ Loading the original photo...  (Muted)   │  note_label
│ [Full size] [Open the file]      [Close] │  zoom_button, open_button, close_button
└──────────────────────────────────────────┘
```

1. **Size.** It opens at once with the PNG preview it was given, sized to the photo and at most
   `PHOTO_SCREEN_SHARE` (90%) of the primary screen's width and height. The buttons and the note are
   packed before the picture, so a small window squeezes the picture and never hides Close. With an
   `original`, the picture fills that room (the preview has the original's shape, so nothing moves when
   the original arrives); without one (the film editor's `PhotoWindow`), the preview is shown no larger
   than it is, and the zoom and open buttons aren't shown. Resizing the window fits the picture again,
   100 ms after the last change.
2. **The original.** With an `original`, `after(0)` starts one `bg.run`: on the worker thread,
   `data.share_path("templates/" + original.path)`, then Pillow opens it, `ImageOps.exif_transpose` (as
   the preview was made), `convert("RGB")` unless it is RGB or L, and a copy shrunk to the screen for
   fitting. Nothing of this touches Tk or runs on the Tk thread. Until it is in, the preview shows with
   "Loading the original photo..." and `zoom_button` disabled. No path (no share), a missing file or a file
   Pillow can't read (an artwork PDF): the preview stays, with "Showing the preview: Stripes couldn't read
   the original photo." (`Warn.TLabel`). There is no local copy of originals: each PC caches previews only
   (storage.md §7.3).
3. **Fit and full size.** Fitted, the picture is scaled to the canvas keeping its shape (`fit_size`,
   Lanczos) and centred. `zoom_button` "Full size" (or a double click on the picture, which opens full size
   on the point clicked) shows the original pixel for pixel on a canvas with both scroll bars, opened on the
   middle of the photo; the window grows to what the photo needs, still at most 90% of the screen. The
   picture drags with the mouse, and the wheel scrolls it (Shift+wheel sideways). The note reads "Drag the
   photo to move around it."; the button reads "Fit to window" and brings the earlier size back.
4. **Open the file** (`open_button`): `bg.run` of `data.share_path(...)`, then on Windows
   (`hx_win.is_windows()`) another `bg.run` of `os.startfile(path)`, which opens it in the PC's default
   viewer; an error shows "Stripes couldn't open the file: {error}". Elsewhere it opens nothing and the
   note reads "Opening files works on Windows only. The file is at {path}.". No path: "Stripes can't reach
   the server, so the original can't be opened now."
5. **One window per photo.** With an `original`, the window's key is `f"photo:{original.sha256}"`;
   `show_photo(...)` shows that window when it is open instead of opening a second one. Escape and Close
   close it.

Test attributes: `caption_label`, `canvas`, `xscroll`, `yscroll`, `picture_label` ("No photo" when there
is no picture), `note_label`, `zoom_button`, `open_button`, `close_button`, `image` (the Pillow image on
show), `photo`, `shown_size`, `source`, `full`, `original`.

## 6. The result and confirmation window

### 6.1 Layout

```
┌ Check the new messages ────────────────────────────────────────────────────────────────┐
│ Check the new messages                                                    (Hx.Head)    │
│ Sent by EXAMPLE\jsmith on FLOOR01 at 2026-10-29 14:02.                                 │  sent_label
│ Look at each printer's picture. Wait for the first bags, then confirm each line.        │
│ (amber) Sent from this PC's copy of the templates (as of 2026-10-29 13:40). Check the   │  notes_frame
│         first bags on each machine.                                                     │
│ (amber) The time server didn't answer, so the PC's time was used.                       │
│ ┌ Scroller ────────────────────────────────────────────────────────────────────────────┐ │
│ │ ┌ Line 1 ──────────────────────────────────────────────────────────────────────────┐ │ │
│ │ │ Line 1 Bagger 1                                                   (Hx.Section)    │ │ │
│ │ │ Live and checked twice. Waiting for you to confirm.                               │ │ │
│ │ │ {Picture 640: the second read of the running message}                             │ │ │  pictures[mid]
│ │ │ {Product of USA and Canada}                                   BagText 22 bold     │ │ │  origins[mid]
│ │ │ Before: {USA → USA and Canada}                                (highlighted)       │ │ │
│ │ │ Message EXCO1-PKO01-7K2M-B9 in slot 9.                                            │ │ │
│ │ │ Line 1 Bagger 2: the Send failed. See the red window.                 (Bad)       │ │ │
│ │ │                                  [Something's wrong]  [ Everything is right ]     │ │ │  wrong_buttons, right_buttons
│ │ └──────────────────────────────────────────────────────────────────────────────────┘ │ │
│ └──────────────────────────────────────────────────────────────────────────────────────┘ │
│                                                                              [Close]   │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

1. Machines are grouped by line (`data.current.machines[mid].line_id`), lines by name, machines in the
   result's order. `sent_label` reads "Sent by {user} on {computer} at {time}." (`result.sender`,
   `result.computer`, `when(result.finished_at)`).
2. Notes at the top (`notes_frame`, `Warn.TLabel`): `result.from_local_copy` → "Sent from this PC's copy
   of the templates (as of {time}). Check the first bags on each machine." (`time` =
   `when(result.data_as_of)`); `result.time_source == "pc"` → "The time server didn't answer, so the
   PC's time was used."; `result.log_queued` → "The send log is waiting on this PC. It uploads when the
   server is back."
3. For each machine with `ok`:

   | Widget | Content |
   |---|---|
   | name | machine name (`Hx.Section.TLabel`) |
   | state label | pending: "Live and checked twice. Waiting for you to confirm."; else the line state's text (§6.2) |
   | note labels (`Warn.TLabel`) | `text(hx_message.NOTE_TEXTS[n])` for each of `m.notes` that is a key of `NOTE_TEXTS` |
   | `pictures[mid]` (`PictureView`, width 640) | `m.readbacks[-1].preview_bmp` (the last running-message read; plan: "Stripes keeps the second picture") |
   | `origins[mid]` (`BagText` 22 bold, `wrap_px` 900) | `m.origin_line`; "(origin unknown)" beside it when `""` |
   | change | `origin_change_text(m.previous.origin_line if m.previous else "", m.origin_line)`: non-empty → "Before:" + highlighted `BagText` 11; `""` → "Same origin as before." |
   | message label (`Hx.Small.TLabel`) | `Msg(N_("Message {name} in slot {slot}."), {"name": m.message_name, "slot": m.slot})` (the states.md text) |

4. A machine without `ok`: "{machine}: the Send failed. See the red window." (`Bad.TLabel`).
5. Line buttons: shown when `same_user(result.sender, identity.user)` and the line has machines in
   `pending` (from `fill`; for a fresh result every `ok` machine is pending). Otherwise the line shows
   "Only {user} can confirm this Send." (`user` = `result.sender`) when it has pending machines.

### 6.2 Confirming

`line_state[line_id]`: `"pending"`, `"saving"`, `"confirmed"`, `"rejected"`, `"stopped"` or
`"none"` (nothing to confirm).

**Everything is right** (`right_buttons[line_id]`, `Hx.Big.TButton`):

1. `line_state = "saving"`, both buttons disabled.
2. `bg.run(lambda: services.pipeline().confirm(send_id, ids, True))`, `ids` = the line's pending machines.
3. Done: `"confirmed"`; the buttons are replaced by "Confirmed by you at {time}." (`Ok.TLabel`, `when(
   clock.time())`). Error: `text(e.msg)` under the line (`Bad.TLabel`), `"pending"`, buttons enabled.
   A `ShareError` for a Send made without the share (any machine of the result has the note `no_lock`)
   also calls `nav.note_offline_answer(send_id, ids, True)`, so the answer reaches the expected states
   when the share is back (§15.3 item 3); the same holds for "Something's wrong" with `False`.

**Something's wrong** (`wrong_buttons[line_id]`):

1. `line_state = "saving"`; `bg.run(lambda: pipeline.confirm(send_id, ids, False))`. The rejection is
   recorded at once (architecture §8: "Something's wrong" → red, `rejected`, `resume_blocked`; the
   sender and the notify user are alerted), before any question.
2. Done: `"rejected"`; the line shows "You said something's wrong. These machines stay red until a new
   Send." (`Bad.TLabel`) and two buttons, "Stop printing..." and "Fix and send again", then opens
   `StopDialog(root, services, machine_ids=ids, reason="rejected", send_id=send_id,
   request=self.request)` at once (§8.1).
3. "Stop printing..." opens that dialog again. "Fix and send again" calls `nav.open_send(prefill=
   SendRequest(send_id="", sender=..., computer=..., created_at=..., lines=[the request's LineChoice for
   this line]))`. When `self.request` is None, the button first loads it with
   `bg.run(data.find_send(send_id))` and `request_from_log`.
4. When the `StopDialog` reports the machines stopped, `line_state = "stopped"` and the line reads
   "Printing stopped. This line needs a new Send." (the dialog calls `w.stopped(line_id)` on this window
   when it is open).

### 6.3 Closing

`close()`: while any line is `"saving"`, `tell(Msg(N_("Wait until the confirmation is saved.")))` and
stay. Otherwise, when this user is the sender and any line is still `"pending"`, `tell(Msg(N_("Lines you
haven't confirmed stay amber \"Send not confirmed\". You can confirm them later from the machine's card
on the MapleJet tab.")))` once, then `destroy()` (plan: "If they close the window, they reopen it from
their monitor card").

### 6.4 Reopening from a card: from_log

`ResultWindow.from_log(master, services, send_id)`:

1. If `nav.window(f"result:{send_id}")` exists: `show()` it and return None.
2. Build the window with an empty result (`SendResult(send_id=send_id, sender="", computer="",
   started_at=0.0, finished_at=0.0, machines=[], time_source="server")`); it shows "Loading the Send..."
   until filled.
3. `bg.run(work)`: `entry = data.find_send(send_id)`; `pending = pipeline.pending_confirmation(send_id)`;
   without an entry, also `pictures = {mid: store.read_preview(mid) for mid in pending}`.
4. Done with an entry: `fill(result_from_log(entry), request=request_from_log(entry), pending=pending)`.
   Machines of the result that aren't pending show their current headline from the board instead of a
   state label, and their line gets no buttons when none of its machines is pending.
5. Done without an entry (the PC that sent crashed before writing it, or the log can't be reached):
   `pending` then comes from the expected states (send.md §11.3 falls back to them), so the window
   lists the pending machines with the latest running picture (`pictures`), the origin from their
   `MachineView.origin_line`, the note "This PC can't find the full record of this Send. These are the
   printers' latest pictures." and the same line buttons, which confirm with `send_id` (send.md §11.2
   accepts that without the entry). The sender check uses `MachineView.confirm_pending_by`
   (`ExpectedState.sender`).
6. `pending` empty: "Nothing in this Send is waiting for confirmation." and Close.
7. Returns the window.

Test attributes: `result`, `request`, `pending`, `line_state`, `pictures`, `origins`, `right_buttons`,
`wrong_buttons`, `notes_frame`.

## 7. The red failure window

### 7.1 What opens it

`nav.open_failure(result, plan)` opens `FailureWindow(root, services, result, plan)` (one per send_id)
after a Send in which any machine failed (§5.8). It shows `rows = hx_send.failure_rows(result, plan)`,
which send.md defines with every text the window needs.

### 7.2 Layout

Full screen (`attributes("-fullscreen", True)`, `attributes("-topmost", True)`), background `RED_BG`,
all text `RED_FG` in plain `tk.Label`s, content in a `Scroller` with background `RED_BG`.

```
████████████████████████████████████████████████████████████████████████████████████████
█ Send failed                                                     (UI_FONT, 28, bold)  █  heading
█ Sent by EXAMPLE\jsmith at 2026-10-29 14:02. Stripes stopped at the step that failed  █
█ and did nothing else.                                           (UI_FONT, 14)        █
█ ┌ one block per FailureRow (border 2 px white) ────────────────────────────────────┐ █
█ │ Line 1 Bagger 2 · Line 1                                    (UI_FONT, 20, bold)   │ █  machine_name, line_name
█ │ Failed at: Check before going live                          (UI_FONT, 16)         │ █  step_text
█ │ The printer's copy of the new message differs from what was sent.  (14)          │ █  msg
█ │ This machine may be printing the new message, the old one, or nothing.            │ █  after_switch_text
█ │ Check the bags now.                                         (UI_FONT, 20, bold)   │ █  (after the switch only)
█ │ ┌ differences (ttk.Treeview, 6 rows) ──────────────────────────────────────────┐ │ █
█ │ │ Where           What   Sent                      Read back                    │ │ █
█ │ │ item 2 (CTEXT)  TXT    Product of USA and Canada Product of USA               │ │ █
█ │ └──────────────────────────────────────────────────────────────────────────────┘ │ █
█ │ Last confirmed printing: EXCO1-PKO01-7K2M-B9                (UI_FONT, 14)         │ █  previous_name
█ │ {Picture 768, white frame}                                                        │ █  pictures[mid]
█ │ {Product of USA}                               BagText 24 bold, white on red      │ █  origins[mid]
█ └──────────────────────────────────────────────────────────────────────────────────┘ █
█ Line 1 Bagger 1 went live. Confirm it in the result window.     (UI_FONT, 14)        █  passed_label
█                                              [  Try again  ]   [  Leave it  ]        █
████████████████████████████████████████████████████████████████████████████████████████
```

1. The heading is "Send failed", then "Sent by {user} at {time}. Stripes stopped at the step that
   failed and did nothing else." (`result.sender`, `when(result.finished_at)`).
2. Each block: `row.machine_name · row.line_name`; "Failed at: {step}" with `text(row.step_text)`;
   `text(row.msg)`; when `row.after_switch`, `text(row.after_switch_text)` in 20 pt bold (the plan's
   "This machine may be printing the new message, the old one, or nothing. Check the bags now.").
3. Differences, when `row.differences` isn't empty: a `ttk.Treeview` with headings "Where", "What",
   "Sent", "Read back" and one row per `Difference` (`where`, `field`, `sent`, `read`: data and bag text
   in cells).
4. What it was last confirmed printing (plan): "Last confirmed printing: {name}" (`row.previous_name`)
   when `row.previous_confirmed`, else "Printing before this Send (not confirmed): {name}", because that
   message may never have been confirmed, may be in drift or may not come from Stripes; "Stripes
   doesn't know what this machine was last confirmed printing." when the name is `""`; a
   `PictureView` (width 768) with `row.previous_preview`; a `BagText` (24, bold, foreground `RED_FG`,
   background `RED_BG`) with `row.previous_origin`, or "(origin unknown)".
5. `passed_label`, when some machines passed: "{machine} went live. Confirm it in the result window." for
   one machine, "{machines} went live. Confirm them in the result window." for more.
6. Buttons `try_button` "Try again" and `leave_button` "Leave it" (`Hx.Big.TButton`), bottom right,
   shown when `same_user(result.sender, identity.user)` and `roles.sender`; otherwise "Only {user} can
   choose Try again or Leave it." (Never the case on the PC that sent, where this window opens.)

### 7.3 Closing

The window waits for Try again or Leave it (plan). `close()` (the close box; there is no other way out)
calls `tell(Msg(N_("Choose Try again or Leave it.")), kind="warning")` and stays. Escape does nothing.

### 7.4 Try again and Leave it

**Try again**: a fresh Send of the failed machines from step 2, never a resume halfway (plan):

1. `ids = [row.machine_id for row in rows]`; `new_plan = plan.restricted(ids)` (a new send_id, the same
   choices, send.md §4.5).
2. `w = run_send(root, services, new_plan)` (§5.9): its countdown and Cancel work as in §5.7. None (the
   sender kept the open Send window's choices, or a Send is running): the red window stays as it is.
3. Otherwise the red window is withdrawn (not destroyed) and subscribes to the retry's events
   (`retry_send_id = new_plan.send_id`):
   - `SendEvent(machine_id="", step=STEP_LOCK, status="ok")` for the retry (its "go"): `self.destroy()`.
     From here the retry's own windows take over.
   - `WorkFailed` with `ref == retry_send_id` (a `PlanError` or `NotAllowed` during the countdown, such
     as "The templates changed ..."), or a `SendFinished` whose result `is_cancelled`: the red window
     is shown again (`deiconify`, full screen, topmost) with `text(msg)` (or "Try again was
     cancelled.") above its buttons, and waits for Try again or Leave it as before, so the line is never
     left red without the red window.

**Leave it**:

1. `ask(Msg(N_("Leave {machines} red? The line stays red until a Send succeeds, the line's notify user
   is alerted, and this is logged.")), yes=N_("Leave it"), no=N_("Go back"))`.
2. Yes: both buttons disabled; `bg.run(lambda: services.pipeline().leave_it(result.send_id, ids))`.
3. Done: `destroy()`. The poller then reads what is really printing (send.md: a new `reread_token`).
   Error: `text(e.msg)` in white bold above the buttons; buttons enabled.

Test attributes: `rows`, `try_button`, `leave_button`, `pictures`, `origins`, `passed_label`.

## 8. Something's wrong, Stop, Resume and Set clocks

All three dialogs make the action's id first, `self.action_id = new_id("ac")`, and run the action on
`start_thread(services, f"hx-action-{action_id}", action_id, target)` with `target` = the
`PrinterActions` call with `on_event=services.bus.post` and `action_id=self.action_id` (architecture
§6.7). The thread name, the `WorkFailed` ref and the event filter (`ActionEvent.action_id`,
`ActionFinished.result.action_id`) are that id from the start. A Try again makes a new id. Each marks
itself running (§2.1 rule 9) with "Stripes is stopping printing on {machines}.", "Stripes is resuming
printing on {machines}." or "Stripes is setting the clocks on {machines}." Machine names come from
`data.current.machines`.

### 8.1 Stop (and the Something's wrong flow)

`StopDialog(master, services, *, machine_ids, reason="user", send_id="", request=None)`;
`state`: `"ask"` → `"working"` → `"done"` or `"failed"`.

```
ask:
┌ Stop printing ──────────────────────────────────────────────────────────────┐
│ Stop printing on Line 2 Bagger 3 and Line 2 Bagger 4?           (Hx.Head)   │  question_label
│ You said something's wrong with the new message. Printing is turned off and │  (reason "rejected")
│ read back twice. Resume stays blocked for these machines, so the rejected   │
│ message can't be restarted by accident.                                     │
│ Printing is turned off and read back twice to make sure it's off.           │  (reason "user")
│                                          [Go back]   [ Stop printing ]      │  back_button, stop_button
└─────────────────────────────────────────────────────────────────────────────┘
working: "Stopping printing..." and one row per machine (mark, text(event.msg)) as in §5.7
done (reason "rejected"):
│ Printing stopped on Line 2 Bagger 3 and Line 2 Bagger 4. Line 2 needs a new Send. │
│                                   [Close]   [ Fix and send again ]               │  close_button, fix_button
done (reason "user"):
│ Printing stopped on Line 1 Bagger 1.                                  [Close]    │
```

1. The ask page: `question_label` "Stop printing on {machines}?" (`join_names` of the machine names),
   then for reason `"rejected"` "You said something's wrong with the new message. Printing is turned
   off and read back twice. Resume stays blocked for these machines, so the rejected message can't be
   restarted by accident." and for reason `"user"` "Printing is turned off and read back twice to make
   sure it's off."
2. **Stop printing**: `state = "working"`; `actions.stop(machine_ids, bus.post, reason=reason,
   send_id=send_id)` on the action thread.
3. **Go back**, or closing on the ask page: `destroy()`. After a rejection the machines stay red
   "Rejected by {user}" with Stop and Send on their cards (states.md `rejected`), and the result window
   still offers "Stop printing...".
4. `ActionFinished`: every machine ok → `state = "done"` and the done text: reason `"rejected"`:
   "Printing stopped on {machines}. {line} needs a new Send." (`line` = `join_names` of the machines'
   line names; it is one line when the dialog comes from the result window, which confirms per line)
   with the main button `fix_button` "Fix and send again" (plan); reason `"user"`: "Printing stopped
   on {machines}." For `"rejected"` it also calls `stopped(line_id)` on the open `ResultWindow` of
   `send_id`.
5. **Fix and send again**: `nav.open_send(prefill=<request restricted to the lines of these machines>)`,
   then `destroy()`. Without a `request`, it is loaded first with `bg.run(data.find_send(send_id))` and
   `request_from_log`; if that finds nothing, `nav.open_send(line_ids=<their lines>)`.
6. **Close** on the done page: `destroy()`. The line stays red "Stopped, needs a Send" and its notify
   user is alerted (plan; the `send_rejected` alert of states.md continues).
7. Any machine not ok → `state = "failed"`, and the dialog becomes the red window (plan: "If stopping
   fails, the red window tells the operator to stop it at the printer with its Print On/Off button
   now, with Try again"): full screen, topmost, `RED_BG`:

   ```
   ████████████████████████████████████████████████████████████████████████
   █ Stop it at the printer now                       (UI_FONT, 28, bold) █
   █ Stripes couldn't stop printing on Line 2 Bagger 4.     (UI_FONT, 16) █
   █ Press Print On/Off on that printer now. Its card shows "Stop it at   █
   █ the printer" until Stripes reads that printing is off.               █
   █ Line 2 Bagger 4: <text(result.msg)>                                  █
   █ Printing stopped on Line 2 Bagger 3.            (when some stopped)  █
   █                                      [ Try again ]   [ Close ]        █
   ████████████████████████████████████████████████████████████████████████
   ```

   Texts: "Stop it at the printer now", "Stripes couldn't stop printing on {machines}." (the failed
   ones), "Press Print On/Off on that printer now. Its card shows "Stop it at the printer" until Stripes
   reads that printing is off.", "{machine}: {problem}" per failed machine, and "Printing stopped on
   {machines}." for the ones that did stop. "Try again" stops the failed machines again (same reason
   and send_id) and returns to `"working"`; "Close" destroys it. On this page `close()` asks nothing.
8. `close()` while `"working"`: `tell(Msg(N_("Wait until Stripes has finished stopping printing.")))`.

Test attributes: `state`, `question_label`, `stop_button`, `back_button`, `fix_button`,
`close_button`, `rows`, `try_button`.

### 8.2 Resume

`ResumeDialog(master, services, *, machine_ids)`. Resume first loads the message on each printer and
shows its picture and origin to confirm, and refuses when it differs from the last confirmed Send or
the last Send was rejected (plan; send.md owns the check).

```
┌ Resume printing ─────────────────────────────────────────────────────────────────────┐
│ Resume printing                                                         (Hx.Head)    │
│ Loading what's on each printer...                    (until every check is back)     │
│ Line 1 Bagger 1                                                                       │
│ {Picture 640}                                                                         │  pictures[mid]
│ {Product of USA and Canada}                                       BagText 20 bold    │  origins[mid]
│ This is the message of the last confirmed Send.                      (Ok)            │
│ Line 1 Bagger 2: <text(check.msg)>                                    (Bad)          │
│                                   [Go back]   [ Resume printing ]                     │  resume_button
└───────────────────────────────────────────────────────────────────────────────────────┘
```

1. After `0` ms the dialog runs one `bg.run` that calls `actions.check_resume(mid)` for each machine in
   turn and returns the `ResumeCheck`s (each holds the lock briefly and reads the printer).
2. Each check: picture `check.design.preview_bmp` (grey box when None), `check.origin_line` in a
   `BagText` or "(origin unknown)", and either "This is the message of the last confirmed Send."
   (`allowed`) or `text(check.msg)`.
3. `resume_button` "Resume printing" is enabled when any check is allowed; it resumes only the allowed
   machines, and when some were refused the dialog says "Only the machines that passed the check are
   resumed." `actions.resume(allowed_ids, bus.post)` on the action thread.
4. `ActionFinished`: "Printing resumed on {machines}." and, per failed machine, "{machine}: {problem}"
   (`Bad.TLabel`); a Close button. A failed resume leaves the machine stopped, so there is no red page.

Test attributes: `checks`, `pictures`, `origins`, `resume_button`, `state`.

### 8.3 Set clocks

`SetClocksDialog(master, services, *, line_ids)`. Sets every printer's clock from the time server,
without a Send (plan). Machines: the active machines of those lines whose printer is set up (active,
`token != ""`).

```
┌ Set clocks ─────────────────────────────────────────────────────────────────────┐
│ Set the clock of every printer on Line 1 and Line 2 from the time server?       │
│ Stripes sets the date, the time and the midnight date change. Nothing else on   │
│ the printers changes.                                                           │
│   Line 1 Bagger 1   Line 1 Bagger 2   Line 2 Bagger 3                            │
│                                         [Go back]   [ Set clocks ]              │  set_button
└──────────────────────────────────────────────────────────────────────────────────┘
done: one row per machine: "✓ Clock set" or "✗ <text(msg)>"; per machine with the note "pc_time":
"The time server didn't answer, so the PC's time was used." (Warn); [Close]
```

The question is "Set the clock of every printer on {lines} from the time server?" (`join_names` of the
line names) with "Stripes sets the date, the time and the midnight date change. Nothing else on the
printers changes." `set_button` "Set clocks" runs `actions.set_clocks(machine_ids, bus.post)` on the
action thread; rows update from `ActionEvent`s.
"Set all clocks..." on the tab passes every active line.

Test attributes: `machine_ids`, `set_button`, `rows`, `state`.

## 9. Line cards, the MapleJet monitor and alerts

### 9.1 LineBoard and its cards

`LineBoard(master, services)` shows a `BoardView` as one card per line (states §8.2). It is embedded in
the MapleJet tab and in `MonitorWindow`. It subscribes to `BoardUpdate` itself (and unsubscribes in its
`destroy()`), and shows `watcher.latest` when built.

1. A `Scroller` (background `PAGE_BG`) holds the `LineCard`s in a grid: 2 columns when the board is at
   least 1100 px wide, else 1, re-gridded on `<Configure>` only when the count changes. Order: the
   board's order (lines by natural name, machines by natural name), never re-sorted by state.
2. `show(board)`: keeps `board` and `received = services.clock.monotonic()`. If the lines or their
   machine ids differ from the cards', every card is rebuilt; otherwise each card and row is updated in
   place (no flicker).
3. A 1 s timer (`after`, cancelled in `destroy`) updates every age text while the board is viewable:
   `text(checked_text(age + elapsed))` with `elapsed = clock.monotonic() - received` and `age` the
   view's `checked_age_s` (None stays "Not checked yet"), as states §5.7 asks.
4. No board yet (`watcher.latest` is None): "Checking..." in the scroller.

```
LineCard (tk.Frame, CARD_BG, 1 px RULE_FG border)
┌─┬──────────────────────────────────────────────────────────────────────────────┐
│█│ Line 1                                         Printing · checked 3 s ago    │  name_label, headline_label, age_label
│█│ [Send...] [Stop line...] [Resume line...] [Set clocks...] [Acknowledge]       │  line buttons
│█│ ──────────────────────────────────────────────────────────────────────────── │
│█│ ● Line 1 Bagger 1   Printing                               checked 3 s ago   │  MachineRow
│█│   {Product of USA and Canada}                                 BagText 13 bold │
│█│   Confirmed by EXAMPLE\jsmith.                                                │  details (Hx.Small)
│█│   Message EXCO1-PKO01-7K2M-B9 in slot 9.                                      │
│█│   [Stop...]                                                                   │
│█│ ● Line 1 Bagger 2   Send failed: still printing the previous message          │
│█│   {Product of USA}                                                            │
│█│   ♪ Send failed on Line 1 Bagger 2 · not acknowledged                         │  alert lines
│█│   [Stop...] [Send...] [Acknowledge]                                           │
└─┴──────────────────────────────────────────────────────────────────────────────┘
 colour bar: 6 px, COLOUR_HEX[line.colour]
```

**Line card**:

| Part | Content |
|---|---|
| colour bar (`tk.Frame`, width 6) | `COLOUR_HEX[line.colour]` |
| `name_label` | `line.name`, (UI_FONT, 13, "bold") |
| `headline_label` | `text(line.headline)` in `COLOUR_HEX[line.colour]` |
| `age_label` | `checked_text` of `line.checked_age_s` (the oldest machine's) |
| not monitored (`line.monitored` False) | every text of the card in `DIM_FG`; the headline already reads "NOT MONITORED" (states §5.4) |

Line buttons, shown by role (§2.7) and enabled by state:

| Button (attribute) | Shown to | Enabled when | Does |
|---|---|---|---|
| `send_button` "Send..." | senders | always; `Hx.Main.TButton` when any machine has `needs_send` | `nav.open_send(line_ids=[line_id])` |
| `stop_button` "Stop line..." | senders | any machine `can_stop` | `nav.stop(<machines with can_stop>)` |
| `resume_button` "Resume line..." | senders | any machine `can_resume` | `nav.resume(<machines with can_resume>)` |
| `clocks_button` "Set clocks..." | senders | any machine not BLUE | `nav.set_clocks([line_id])` |
| `ack_button` "Acknowledge" | `roles.acknowledge` | any active alert without `acked_by` on the line | `watcher.acknowledge(<those alert ids>)` |

**Machine row** (`MachineRow`):

| Part | Content |
|---|---|
| dot (`tk.Canvas` 14x14) | a circle in `COLOUR_HEX[view.colour]` |
| `name_label` | `view.name`, (UI_FONT, 11, "bold") |
| `headline_label` | `text(view.headline)` in the view's colour |
| `age_label` | `text(checked_text(...))`, `Hx.Small.TLabel`, right |
| `origin` (`BagText` 13 bold, `wrap_px` 520) | `view.origin_line`; when `""`, `unknown_label` "(origin unknown)" shows instead |
| `details_label` | `text(d)` for each `view.details`, one per line, `Hx.Small.TLabel`; a detail whose text equals an alert line's message is left out (no duplicates) |
| alert lines | one per `view.alerts`: "♪ " when `alert.sound`, then `text(alert.msg)`, then " · " and "acknowledged by {user}" (`acked_by`) or "not acknowledged" |

Machine buttons: a BLUE machine shows none (states §8.2). Otherwise each shows only when its flag
allows it, so rows stay short:

| Button (attribute) | Shown when | Does |
|---|---|---|
| `stop_button` "Stop..." | sender and `view.can_stop` | `nav.stop([machine_id])` |
| `resume_button` "Resume..." | sender and `view.can_resume` | `nav.resume([machine_id])` |
| `send_button` "Send..." (`Hx.Main.TButton`) | sender and `view.needs_send` | `nav.open_send(line_ids=[line_id])` |
| `confirm_button` "Confirm..." (`Hx.Main.TButton`) | `view.confirm_pending_by` and `same_user(view.confirm_pending_by, identity.user)` | `nav.open_result(view.confirm_send_id)` |
| `ack_button` "Acknowledge" | `roles.acknowledge` and any alert without `acked_by` | `watcher.acknowledge(<the machine's unacknowledged alert ids>)` |
| `setup_button` "Set up..." | administrator and `view.code == "not_set_up"` | `nav.open_admin(page="machines", record_id=machine_id)` |

A grey `off` machine reads "Printer off" with no alert (states §8.2 rule 4): nothing special is drawn.

### 9.2 The MapleJet monitor

`MonitorWindow(root, services)`, key `"monitor"`, title "MapleJet monitor":

```
┌ MapleJet monitor ─────────────────────────────────────────────────────────────────┐
│ MapleJet monitor                                       [Floor board] [Refresh now] │  heading, floor_button, refresh_button
│ 1 red · 1 amber · 4 green · updated 2:05:13 PM                                     │  summary_label
│ Server unreachable: showing this PC's own checks · next check in 18 s              │  banner_label (when present)
│ LineBoard                                                                          │  board
└────────────────────────────────────────────────────────────────────────────────────┘
```

1. `summary_label`: `board_summary(board)` joined with " · ", then `tr("updated {time}", time=...)` (the
   existing Zebra monitor text) with `time = datetime.now().strftime("%I:%M:%S %p").lstrip("0")`.
   `board_summary` counts machines by colour in the order RED, AMBER, BLUE, GREY, GREEN and returns, for
   each non-zero count, "{n} red", "{n} amber", "{n} in progress", "{n} grey", "{n} green".
2. `refresh_button` "Refresh now": in standalone mode `poller.refresh_now()` (False: "You can refresh
   once every {seconds} s." under the summary for 3 s); otherwise `watcher.refresh_now()`.
3. `floor_button` "Floor board": `nav.open_floor_board()`.
4. Opening and closing it call `nav.update_monitor_flag()` (§15.3), so this PC keeps polling in
   standalone mode while it is open (plan).

### 9.3 AlertSounder

`AlertSounder(services)` is made by `start_services` and kept in `nav.sounder`. It runs whenever the
services run, including while Stripes is hidden in the tray (states §4.4 rule 3).

1. It subscribes to `BoardUpdate` and calls `evaluate(event.board)`. `nav` also calls
   `evaluate(watcher.latest)` when the floor board opens or closes.
2. `evaluate(board)`: `ringing = hx_state.sounding_alerts(board, identity, floor_board=
   nav.floor_board_open())` (`[]` when board is None).
   - `ringing` non-empty and not `playing`: `hx_win.play_alert(repeat=True)`, `playing = True`.
   - `ringing` empty and `playing`: `hx_win.stop_alert()`, `playing = False`.
   - A new alert while ringing doesn't restart the sound; there is no snooze (states §4.4 rule 4).
3. Personal pop-up: `personal` = the alerts of `ringing` with a `"user:<name>"` recipient where
   `same_user(name, identity.user)`. When `personal` holds an id not in `shown_ids`, the `AlertPopup`
   is opened (or updated and shown) with every `personal` alert, and the ids are added to `shown_ids`.
   When `personal` becomes empty the pop-up closes itself.
4. After this PC acknowledges, the watcher posts a new `BoardUpdate` at once (states §4.5), so the sound
   stops here immediately and on other PCs within about `watch_s`.
5. `stop()`: `stop_alert()` if playing, unsubscribe, close the pop-up. `stop_services` calls it.

### 9.4 The alert sound file

`assets/alert.wav` is exactly `alert_wav_bytes()`, written once by the builder
(`python -c "import hx_ui_common, pathlib; pathlib.Path('assets/alert.wav').write_bytes(hx_ui_common.alert_wav_bytes())"`)
and committed. It meets states §4.4: 3.0 s long, a two-tone chime of 0.8 s (higher, then lower) and
silence, looped by `SND_LOOP` so it repeats every 3 s.

1. PCM, mono, 16-bit signed little-endian, 22,050 Hz, 66,150 frames (3.000 s), written with the
   standard `wave` module (`setnchannels(1)`, `setsampwidth(2)`, `setframerate(22050)`, then
   `writeframes`) into a `BytesIO`; no other chunks.
2. Frame `i` at `t = i / 22050` s: tone A, 880 Hz, for `0 <= t < 0.35`; tone B, 660 Hz, for
   `0.40 <= t < 0.80`; silence elsewhere. In a tone from `t0` to `t1`: `env = min(1, (t - t0) / 0.01,
   (t1 - t) / 0.01)` and the sample is `round(0.45 * 32767 * env * math.sin(2 * math.pi * f * (t - t0)))`,
   packed with `struct.pack("<h", ...)`.
3. The test compares the decoded file with `alert_wav_bytes()` sample by sample, allowing a difference
   of 1 (`math.sin` may differ in the last bit between platforms), and checks 22,050 Hz, 1 channel, 2
   bytes and 66,150 frames.

### 9.5 The personal alert pop-up

`AlertPopup(root, services)`, key `"alerts"`, title "MapleJet alert". It shows the alerts that ring for
this user (§9.3 rule 3), so a notify user signed in anywhere sees why the PC is ringing, even with
Stripes in the tray.

```
┌ MapleJet alert ──────────────────────────────────────────────────────┐
│ MapleJet alert                                          (Hx.Head)    │
│ ♪ Send failed on Line 1 Bagger 2                  since 2026-10-29 14:03 │  alert_labels
│ ♪ Line 1 Bagger 1 isn't printing what Stripes sent  since ... 14:05  │
│ The sound repeats until someone acknowledges.                        │
│ [Open MapleJet monitor]   [Close]                 [ Acknowledge ]    │  monitor_button, close_button, ack_button
└──────────────────────────────────────────────────────────────────────┘
```

1. Each line: "♪ " + `text(alert.msg)` + "since {time}" (`when(alert.started_at)`).
2. `ack_button` "Acknowledge" (`roles.acknowledge`): `watcher.acknowledge(<listed ids without
   acked_by>)`. Without the role: "Ask a Stripes MapleJet Sender or a Stripes Administrator to
   acknowledge it." instead of the button.
3. `monitor_button` "Open MapleJet monitor": `nav.open_monitor()`. `close_button` "Close": `destroy()`;
   the sound goes on, and the pop-up comes back only for a new alert.
4. It is shown with `show()` (raised and topmost for 200 ms), never kept topmost.

## 10. The floor board

### 10.1 Opening

`nav.open_floor_board()` opens one `FloorBoard(root, services, screen=settings.floor_board_screen,
size=settings.floor_board_size)` (key `"floor"`), then `nav.update_monitor_flag()` (so
`poller.set_monitor_open(True, floor_board=True)`: this PC polls in standalone mode and writes
`floor_board` in its heartbeat, states §8.1) and `nav.sounder.evaluate(watcher.latest)` (the floor
board rings for `"floor"` alerts). Closing it does the same in reverse.

### 10.2 Screen and size

1. `mons = hx_win.monitors()`. The screen is `mons[screen]` when `0 <= screen < len(mons)`, else
   `mons[0]`; with no list (off Windows) it is the Tk screen: `Monitor(index=0, x=0, y=0,
   width=root.winfo_screenwidth(), height=root.winfo_screenheight(), primary=True)`. A missing screen
   falls back to 0 (states §8.1).
2. `size == "full"`: full screen with no title bar on that screen: `overrideredirect(True)` and
   `geometry(f"{m.width}x{m.height}+{m.x}+{m.y}")`. Not topmost: the production-schedule PC runs other
   programs too.
3. `size == "<w>x<h>"`: a normal window titled "MapleJet floor board" at the screen's top-left,
   `geometry(f"{min(w, m.width)}x{min(h, m.height)}+{m.x}+{m.y}")`.
4. Any other value is treated as `"full"` (`HxSettings.load` already cleans it, §14).

### 10.3 Drawing

The board is two `tk.Canvas`es (background `CARD_BG`, `highlightthickness=0`) filling the window:
`header`, `header_h` tall, and under it `canvas`, which holds the rows and scrolls when they can't all
fit, with a vertical `ttk.Scrollbar` (`scrollbar`) at its right shown only then. The window is
`hx_no_translate`, and the origins are bag text drawn as canvas text, so no language switch can touch
them. Both are redrawn completely on `<Configure>` of `canvas` (debounced 100 ms), on every
`BoardUpdate` and `DataUpdate` and on `retranslate()`; a redraw keeps the scroll position. Ages and the
clock change every 1 s, and the colour bars flash every `FLOOR_FLASH_MS`, by `itemconfigure` only.

The rows are grouped under their line's name, and every row has the same fixed columns: name, origin,
status, age. Nothing wraps and nothing runs into the next column: a name, status or age too long for
its column shrinks, then ends in "…"; an origin is never shortened, it shrinks instead, and every
origin on the board is drawn at one size, so the rows look even. All rows fit, or the board scrolls.
(This replaces the build's first layout, which put the line's name above each machine's full name,
sized each origin on its own row, wrapped long texts and never scrolled; where states.md §8.1 still
describes that, this section is the one to follow.)

```
┌─────────────────────────────────────────────────────────────────────────────────────────────────┐
│ MapleJet floor board  2:05 PM  Server unreachable: ...  [Refresh now][Acknowledge][Close]       │  header (header_h)
├─────────────────────────────────────────────────────────────────────────────────────────────────┤
│ Line 1                                                                                          │  line band (group_h), PAGE_BG
├──┬────────────┬────────────────────────────────────┬─────────────────────────┬──────────────────┤
│██│ Bagger 1   │ Product of USA and Canada          │ Printing                │ checked 3 s ago  │  one row per machine (row_h)
├──┼────────────┼────────────────────────────────────┼─────────────────────────┼──────────────────┤
│██│ Bagger 2   │ Product of USA                     │ Different origins on t… │ checked 4 s ago  │  1 px between rows of a line
╞══╧════════════╧════════════════════════════════════╧═════════════════════════╧══════════════════╡  2 px above each later band
│ Line 2                                                                                          │
├──┬────────────┬────────────────────────────────────┬─────────────────────────┬──────────────────┤
│░░│ Bagger 3   │ USA (pre-printed)                  │ NOT MONITORED           │ checked 41 s ago │  NOT MONITORED: grey text
└──┴────────────┴────────────────────────────────────┴─────────────────────────┴──────────────────┘
 2.5 %   14 %                     44 %                           27 %                  12.5 %
```

`floor_layout(width, height, rows, lines=0, scrollbar_w=0)` (pure, tested), for `rows` machines under
`lines` line bands:

| Value | Formula |
|---|---|
| `header_h` | `max(48, round(height * 0.07))` |
| `row_h` | `max(room / max(units, FLOOR_MIN_ROWS), FLOOR_MIN_ROW_PX)`, with `room = height - header_h` and `units = rows + FLOOR_GROUP_SHARE * lines` (`FLOOR_MIN_ROWS` 4, `FLOOR_GROUP_SHARE` 0.5, `FLOOR_MIN_ROW_PX` 44): the rows and bands fill the room under the header, a row at most a quarter of it, and rows never get shorter than 44 px |
| `group_h` | `row_h * FLOOR_GROUP_SHARE`: a line's band is half a row |
| `rows_h`, `scrolls` | `round(row_h * units)`; `scrolls` when `rows_h > room` |
| `width` | the rows' width: `width`, less `scrollbar_w` when it scrolls (the window passes `scrollbar.winfo_reqwidth()`) |
| `bar_w`, `names_w`, `origin_w`, `status_w` | `round(width * 0.025)`, `round(width * 0.14)`, `round(width * 0.44)`, `round(width * 0.27)` of the rows' width |
| `age_w` | `width - bar_w - names_w - origin_w - status_w` |
| `header_px` | `max(12, round(header_h * 0.45))` |
| `line_px` | `max(9, round(group_h * 0.60))` |
| `machine_px`, `origin_px`, `status_px`, `age_px` | `max(9, round(row_h * 0.30))`, `max(10, round(row_h * 0.50))`, `max(9, round(row_h * 0.30))`, `max(9, round(row_h * 0.24))`: the largest each column's text is drawn |

Examples: 1920x1080 with 12 machines on 4 lines gives `header_h` 76, `row_h` 71.71, `group_h` 35.86,
`origin_px` 36, `status_px` 22, and everything fits; 1280x800 with 6 machines on 2 lines gives
`header_h` 56, `row_h` 106.29 and columns of 32, 179, 563, 346 and 160 px; 1280x800 fits 14 machines on
4 lines, and with 30 machines on 5 lines it scrolls, with 44 px rows.

Rows come from `floor_board_rows(board)` in their order. Before the first row of each line comes that
line's band, `group_h` tall; each row is `row_h` tall; positions are summed as floats and rounded when
drawn. Fonts are pixel sizes: `(family, -px, weight)`. Each text is vertically centred in its row or
band, is one line (`width` 0), starts 12 px into its column and ends at least 12 px before the next.

**One size per column.** At each redraw every column's text gets one size for all rows, from
`fit_px(texts, measure, start, smallest, room)` (pure): the largest size from `start` down to
`smallest` at which the widest of the column's texts fits `room`, the column's width less 24 px
(`smallest` when none does). `measure(px, text)` is `tkinter.font.Font(...).measure` at that size.

| Column | `start` | `smallest` | Still too wide at `smallest` |
|---|---|---|---|
| name | `machine_px` | 14 | `ellipsize` |
| origin | `origin_px` | 1 | can't happen: origins shrink until the longest fits, and are never cut short |
| status | `status_px` | 16 | `ellipsize` |
| age | `age_px` | 9 | `ellipsize`. Sized for the rows' ages and also "Not checked yet" and "checked 119 s ago"; an age that outgrows its column before the next redraw is shortened by the 1 s tick |

`ellipsize(value, measure, room)` (pure) returns `value` when `measure(value) <= room`, else its longest
start that fits with "…" after it (spaces before the "…" dropped; `""` when not even "…" fits). It is
never used on bag text.

For each line's band (`LineView` `l` from the board):

1. A `PAGE_BG` rectangle the rows' full width; above every band but the first, a 2 px `RULE_FG` line.
2. `line_items[line_id]`: the line's name, (UI_FONT, `-line_px`, bold), `TEXT_FG`, at x 16, ellipsized
   to the width; `DIM_FG` when none of the line's machines is monitored (as the line cards, §9.1).

For each `FloorRow` `r` (and its `MachineView` `v`):

1. **Colour bar** (`bar_items`): a rectangle `bar_w` wide, the full row height, in
   `COLOUR_HEX[r.colour]`; while `v.unacked_sound`, it alternates with `CARD_BG` every `FLOOR_FLASH_MS`
   (states §8.1). A redraw in the "off" half keeps it off until the next beat.
2. **Name** (`name_items`): `short_name(r.machine_name, r.line_name, code)` (pure), bold, `TEXT_FG`. It
   is the machine's name without its line's name in front, when the name starts with the line's name
   as a whole word (any case; the separators after it, such as a space or " - ", dropped): "Line 1
   Bagger 1" under "Line 1" is "Bagger 1", and "Line 10 Bagger 1" stays as it is. When nothing else
   is left, it is the machine's printed code (`Machine.code` from `services.data.current`), and
   without one the whole name.
3. **Origin** (`origin_items[r.machine_id]`): `r.origin_line` in the bag font, bold, at the board's
   one origin size. When `r.origin_line` is `""` the origin item is empty, and "(origin unknown)"
   (`unknown_items`; a translated item, redrawn on `retranslate`) is drawn in its place in `MUTED_FG`,
   normal, at the smaller of the status and origin sizes, ellipsized to the column.
4. **Status** (`status_items`): `text(r.status)`, bold, fill `COLOUR_HEX[r.colour]`.
5. **Age** (`age_items`): `text(checked_text(age))`, normal, `MUTED_FG`, `anchor="e"` at the rows' right
   edge minus 12 px; `age = r.checked_age_s + <seconds since the BoardUpdate>` (None stays None).
6. **NOT MONITORED** rows (`v.code == "not_monitored"`): every text of the row in `DIM_FG`; the bar
   keeps its colour (states keeps a last known red or amber).
7. **Dividers**: a 1 px `#e5e7eb` line between rows of the same line; the bands divide the lines. When
   a line's origins differ, each of its rows shows its own origin (states §5.6), so every origin is
   listed.

**Scrolling.** While `scrolls`, `scrollbar` is packed at the right of `canvas`, `canvas`'s
`scrollregion` is `(0, 0, width, rows_h)` and its `yscrollincrement` half a row; the mouse wheel
anywhere on the board scrolls it (`<MouseWheel>`, `<Button-4>` and `<Button-5>` bound on the window).
Otherwise the scrollbar is hidden, the view is at the top and the wheel does nothing.

Header (`header`, `header_h` tall, bottom border 1 px `RULE_FG`):

1. Left: "MapleJet floor board", (UI_FONT, `-header_px`, bold).
2. Then the time, `time.strftime("%I:%M %p").lstrip("0")`, updated every second.
3. Then `text(board_banner(board))` in `COLOUR_HEX[AMBER]` at `max(8, round(header_px * 0.6))` px when
   it isn't None, ellipsized, also as it counts down, so it stops 16 px short of the buttons.
4. Right, as canvas windows (`create_window`): `refresh_button` "Refresh now" (standalone only;
   `poller.refresh_now()`, refused within `refresh_now_min_s`), `ack_button` "Acknowledge" (only when
   `roles.acknowledge` and `sounding_alerts(board, identity, floor_board=True)` isn't empty; it
   acknowledges those alerts) and `close_button` "Close". Escape does nothing; the board stays until
   Close.
5. No rows: centred in the rows area, (UI_FONT, `-status_px`, bold), `message_item` says "Checking..."
   before the first board, else what `no_lines_texts` says ("No lines are set up yet.", "Checking..."
   while the settings have active lines, "Loading the MapleJet settings..." or the settings' problems,
   these in `COLOUR_HEX[AMBER]`).

Test attributes: `header`, `canvas`, `scrollbar`, `layout: FloorLayout`, `font_px` (the size of each
column: `"name"`, `"origin"`, `"status"`, `"age"`, `"unknown"`), `rows: list[FloorRow]`, `line_items`,
`name_items`, `origin_items`, `unknown_items`, `status_items`, `age_items`, `bar_items` (on `canvas`),
`title_item`, `clock_item`, `banner_item` (on `header`), `message_item` (on `canvas`),
`flash_on: bool`, `refresh_button`, `ack_button`, `close_button`.

## 11. Admin screens

### 11.1 The admin window and the editing pattern

`nav.open_admin(page="", record_id="")` (administrators only) opens one `AdminWindow(root, services,
page=page, record_id=record_id)`, title "MapleJet admin", or shows the open one and calls
`select(page, record_id)`.

```
┌ MapleJet admin ─────────────────────────────────────────────────────────────────────────────┐
│ MapleJet admin                                                               (Hx.Head)      │
│ ! problems_frame: DataService.problems, one per line (Bad)                                   │
│ ! share_banner: The server can't be reached. Changes can't be saved now.  (Warn; when down)  │
│ [Company][Lines][Machines][Printers][Products and films][Print texts][Machine + film]  notebook│
│ ┌ page (AdminPage) ───────────────────────────────────────────────────────────────────────┐ │
│ │ [New line]   [ ] Show not active                                                         │ │  new_button, show_inactive_var
│ │ ┌ tree (ttk.Treeview) ──────────┐ ┌ editor (Scroller) ──────────────────────────────────┐ │ │
│ │ │ Name     Code  Cartridge ...  │ │ form fields (each page's table below)                │ │ │
│ │ │ Line 1   L1    Plastic        │ │                                                      │ │ │
│ │ │ Line 2   L2    Paper (Boxtron)│ │ Revision 2 · saved by EXAMPLE\jsmith at 2026-10-29 … │ │ │  revision_label
│ │ │                               │ │ (Warn) EXAMPLE\akim changed this record ... [Reload]  │ │ │  changed_banner
│ │ │                               │ │ (Bad) problems, one per line                          │ │ │  problems_label
│ │ │                               │ │ (Warn) warnings, one per line                         │ │ │  warnings_label
│ │ │                               │ │                                  [Revert]  [ Save ]   │ │ │  revert_button, save_button
│ │ └───────────────────────────────┘ └──────────────────────────────────────────────────────┘ │ │
│ └─────────────────────────────────────────────────────────────────────────────────────────┘ │
│ Saved Line 1.                                                                  status_label  │
└──────────────────────────────────────────────────────────────────────────────────────────────┘
```

| Page key | Tab | List columns | Record | Checked before saving | New id |
|---|---|---|---|---|---|
| `company` | "Company" | none (one record) | `Company` | `validate_company`; `origin_conflicts` as warnings (§11.2) | `"company"` |
| `lines` | "Lines" | Name, Code, Cartridge, Notify user, Active | `Line` | `validate_line`, `notify_user_problems` | `new_id("ln")` |
| `machines` | "Machines" | Name, Code, Line, Placement, Printer, Active | `Machine` | `validate_machine` | `new_id("mc")` |
| `printers` | "Printers" | Name, IP address, UID, Password, Active | `Printer` | `printer_problems` | `new_id("pr")` |
| `products` | "Products and films" | a tree: product (Name, Customer, Active) → films (Code · revision, Status, Settings revision) | `Product`, `Film` | `product_problems`; `validate_film` | `new_id("pd")`, `new_id("fm")` |
| `print_texts` | "Print texts" | Name, Code, Status, Revision | `PrintText` | `validate_print_text`, `membership_saves` | `new_id("pt")` |
| `adjustments` | "Machine + film" | Machine, Film, Placement, Adjustment, Set by, Set at | `MachineFilmAdjustment` | `adjustment_problems` | `adjustment_id(machine_id, film_id)` |

Every page follows these steps (`AdminPage`):

1. **List.** Records sorted by `natural_key(name)`. `show_inactive_var` ("Show not active", off by
   default) hides records with `active == False` or `status == "inactive"`. The list is refreshed on
   every `DataUpdate`, keeping the selection by id.
2. **Edit.** Selecting a row copies the record (`copy.deepcopy`) into `working`, keeps
   `expected_revision = record.revision`, fills the form and sets `dirty = False`. Every form change
   sets `dirty = True`.
3. **New.** `new_button` ("New line", "New machine", "New printer", "New product", "New print text",
   "New adjustment...") makes a new record with its new id and the dataclass defaults,
   `expected_revision = None`, `dirty = True`; `revision_label` reads "Not saved yet." Records are
   never deleted: there is no delete button anywhere; "Active" or a status of "Not active" retires a
   record (architecture decision 9).
4. **Leaving** a dirty record (another row, another page, closing the window):
   `ask(Msg(N_("Discard your changes to {name}?"), {"name": <the record's name>}), yes=N_("Discard"), no=N_("Keep editing"))`; "Keep
   editing" restores the selection.
5. **Save** (`save_button`):
   1. Read the form into a copy of `working` with `parse_number` for numeric fields. A bad number is a
      problem "{field}: type a number." (or "{field}: type a whole number."); an empty required number
      is "{field} is needed." (`field` = the field's translated label as a nested `Msg`).
   2. Run the page's checks (table above). Any problem: show them in `problems_label`, save nothing.
   3. Warnings that need an answer (§11.2, §11.4, §11.7) are asked now.
   4. `bg.run(lambda: data.save(record, expected_revision=expected_revision))`; `save_button` disabled
      and "Saving..." in `status_label` meanwhile.
   5. Done: `working` = the returned record, `expected_revision` = its revision, `dirty = False`,
      `status_label` "Saved {name}." (the existing `stripes.py` text). The list follows on the next
      `DataUpdate` (the save calls `reload()`).
   6. Error: `RevisionConflict` → `text(e.msg)` in `problems_label` and `reload_button` "Reload", which
      (after the discard question when dirty) loads the record from `data.current`. Every other
      `HxError` (`NotAllowed`, `ShareUnavailable`, `ShareDenied`, `FormatTooNew`, ...) → `text(e.msg)`.
      `ValueError` → "Stripes couldn't save this: {error}". The form keeps the edits.
6. **Revert** (`revert_button`): reload `working` from `data.current` (after the discard question).
7. **Someone else saved.** On `DataUpdate`, when the edited record's revision in the new data is above
   `expected_revision`, `changed_banner` shows "{user} changed this record at {time}. Reload to see
   their change; saving now would be refused." with `reload_button`.
8. **Revision line.** "Revision {n} · saved by {user} at {time}" (`Muted.TLabel`).
9. **Server down.** While `not services.store.available` or `data.current.source == "local"`,
   `share_banner` shows and every `save_button` is disabled (saves must go to the share).
10. **Role lost.** When a `DataUpdate` makes `roles.admin` False, every Save is disabled and the window
    shows "Only Stripes Administrators can change this." (storage.md's text).
11. `select(page, record_id)`: selects the tab and the record (for `products`, a film id selects that
    film under its product), with the leaving question of step 4.

Test attributes: `AdminWindow.notebook`, `pages`; each `AdminPage`: `tree`, `working`,
`expected_revision`, `dirty`, `fields: dict[str, tk.Variable | tk.Widget]` (keyed by the record's field
names), `new_button`, `save_button`, `revert_button`, `reload_button`, `problems_label`,
`warnings_label`, `status_label`, `show_inactive_var`.

### 11.2 Company

```
┌ Company ──────────────────────────────────────────────────────────────────────────────┐
│ Time server           [time.example.local     ]  Fallback  [192.0.2.123    ]           │
│ Countries (spelled exactly as printed)                                                 │
│ ┌ countries_tree ────────────────────────────────────────────┐  [Add country]          │
│ │ Country   Also refused in other text      Offered           │                         │
│ │ USA       US, U.S.A., United States        Yes              │                         │
│ └────────────────────────────────────────────────────────────┘                         │
│   Country [USA     ]  Also refused [US, U.S.A., United States ]  [x] Offered  [Apply]   │  country row editor
│ Inks (the names the printers report)                                                   │
│ ┌ inks_tree ───────────────────────────────────────────────────┐ [Add ink] [Remove ink] │
│ │ Ink name               Cartridge         Colour              │                         │
│ └──────────────────────────────────────────────────────────────┘                         │
│   Ink name [Example Black Plastic ]  [Plastic v]  [Black v]  [Apply]                     │
│ Lot: from [4 ] to [8 ] digits         Free text: at most [30 ] characters               │
│ Administrators group [Stripes Administrators  ]  SID [S-1-5-21-...-2101 ] [Look up]      │
│                      You are in this group.                                              │
│ Senders group        [Stripes MapleJet Senders]  SID [S-1-5-21-...-2102 ] [Look up]      │
│ Standard fonts 1 [CourierN] 2 [Arial] 3 [ArialNarrow] 4 [ArialNarrowB] 5 [TimesNewRom] 6 [LinearBeam] │
│   Change these only when every printer's font list has changed: Stripes refuses to send  │
│   to a printer whose fonts differ.                                                       │
│ Which counter counts bags  (•) Print counter (PMCNT)  ( ) Message counter (MSCNT)        │
│                            ( ) No counter: time each test step                           │
└─────────────────────────────────────────────────────────────────────────────────────────┘
```

| Field | Widget | Notes |
|---|---|---|
| `time_server`, `time_server_fallback` | Entry | a host name or IP address |
| `countries` | `countries_tree` and the row editor | aliases typed comma-separated, stored as a list (stripped, empty ones dropped); a country is never removed: untick "Offered" (`active`) |
| `inks` | `inks_tree` and the row editor; cartridge and colour `ChoiceBox`es | inks may be removed (a lookup list, not a record) |
| `lot_min_digits`, `lot_max_digits` | Spinbox 1–20 | |
| `free_text_max` | Spinbox 1–100 | |
| `admin_group`, `admin_group_sid`, `sender_group`, `sender_group_sid` | Entry each, plus `admin_lookup_button`/`sender_lookup_button` "Look up" | below each SID: "You are in this group." or "You aren't in this group." (`sid in identity.sids`) |
| `standard_fonts` | 6 Entries, "Standard fonts" | hint: "Change these only when every printer's font list has changed: Stripes refuses to send to a printer whose fonts differ." |
| `bag_counter` | 3 Radiobuttons: `PMCNT`, `MSCNT`, `none` | OPEN in architecture §11; default `PMCNT` |

1. **Look up**: `bg.run(lambda: hx_win.lookup_group_sid(name))`; a SID fills the SID entry, None shows
   `tell(Msg(N_("Windows couldn't find a group called {name}."), {"name": name}))`.
2. **Before saving**, after `validate_company`:
   - When `admin_group_sid not in identity.sids`: `ask(Msg(N_("You aren't in {group}. After saving, you
     won't be able to change the MapleJet settings. Save anyway?"), {"group": company.admin_group}),
     yes=N_("Save anyway"),
     no=N_("Go back"))`.
   - `conflicts = origin_conflicts(data, company)`: when not empty, `ask(Msg(N_("With this country
     list, these records contain words about origin and can't be sent until they're fixed:
     {list} Save anyway?")), ...)` with `list` = the conflicts' texts, one per line.
   - These two questions are the "warnings" of step 5.3.

### 11.3 Lines

| Field | Widget | Hint (Muted) |
|---|---|---|
| `name` | Entry | |
| `code` ("Printed code") | Entry (width 5) | "Up to 3 capital letters or digits, printed by a Line code item." |
| `cartridge` ("Cartridge type") | `ChoiceBox`: `paper` "Paper (Boxtron)", `plastic` "Plastic" | "Only films of the same type are offered at Send." |
| `notify_user` ("Notify user") | Entry, plus `me_button` "Me" (fills `identity.user`) | "DOMAIN\name. Gets this line's alerts on whichever PC they're signed in to." |
| `active` | Checkbutton "Active" | |
| machines | a read-only list of `data.line_machines(line_id)` names, and `add_machine_button` "Add a machine" (opens the Machines page with a new machine on this line) | |

`notify_user_problems(text)`: `""` is fine; otherwise it must fullmatch `[^\\/\s][^\\/]*\\[^\\/]+`,
else "Type the notify user as DOMAIN\name, for example EXAMPLE\jsmith."

### 11.4 Machines (placement mode)

```
┌ Machine ──────────────────────────────────────────────────────────────────────────────┐
│ Name            [Line 1 Bagger 1        ]                                              │
│ Printed code    [B1 ]  Up to 3 capital letters or digits, printed by a Machine code item. │
│ Line            [Line 1               v]                                               │
│ Printer         [Printer 1 (192.0.2.10) v]  A printer belongs to one machine.           │
│ Placement mode  (•) Delay    ( ) Encoder                                               │
│   Delay: each film's delay after the trigger, plus this machine's delay adjustment for │
│   that film. No calibration.                                                            │
│ Print speed (cm/min) [2400 ]   Needed before this machine can be sent to.               │
│ [ ] Rotate   [ ] Mirror        Depend on how the printer is mounted.                    │
│ Seconds per bag [2.4 ]         Used to time test steps when no counter counts bags.     │
│ ┌ Encoder (encoder machines only) ────────────────────────────────────────────────────┐ │
│ │ Head distance: not calibrated. This machine can't be sent to until it is. [Calibrate...] │ │
│ │ Expected wheel size (mm) [56  ]   Expected pulses per turn [5000 ]                   │ │
│ │ Stripes reads these from the printer every day and raises an alert if they change.   │ │
│ └─────────────────────────────────────────────────────────────────────────────────────┘ │
│ [x] Active                                                                              │
│ Now: Printing · checked 3 s ago                                                         │
└─────────────────────────────────────────────────────────────────────────────────────────┘
```

| Field | Widget | Notes |
|---|---|---|
| `name`, `code` | Entry | |
| `line_id` | `RecordBox` of active lines | |
| `printer_id` | `RecordBox`, shown as "Printer 1 (192.0.2.10)" | active printers not used by another active machine, plus this machine's current one |
| `placement` | 2 Radiobuttons: `delay` "Delay", `encoder` "Encoder" | the hint under them changes with the choice (texts below) |
| `print_speed` | Entry, label "Print speed (cm/min)" | OPEN units (architecture §11); range 1–2500; empty = None |
| `rotate`, `mirror` | Checkbuttons | |
| `seconds_per_bag` | Entry | OPEN (timed test steps) |
| `head_distance_mm` | read-only label, and `calibrate_button` "Calibrate..." | encoder only; set by calibration (storage.md §13.4 step 8) |
| `encoder_wheel_mm`, `encoder_ppr` | Entries | encoder only |
| `active` | Checkbutton | |

1. Placement hints: delay "Delay: each film's delay after the trigger, plus this machine's delay
   adjustment for that film. No calibration."; encoder "Encoder: each film's print position in mm,
   worked out from this machine's head distance. Calibrate it once, at installation."
2. The encoder frame is disabled for delay machines (values kept).
3. Head distance label: "Head distance: {mm} mm (calibrated by {user} at {time})" from
   `data.calibrations.get(machine_id)` when set; "Head distance: not calibrated. This machine can't be
   sent to until it is." when None.
4. `calibrate_button` opens `CalibrationWindow(self, services, machine_id=...)`; disabled while the
   machine isn't saved or `dirty`.
5. Changing `placement` of a saved machine asks, when saving: `ask(Msg(N_("Change the placement mode of
   {machine}? Other film values and adjustments then apply, so check every film on this machine with
   a test print.")), yes=N_("Change it"), no=N_("Go back"))`.
6. "Now:" shows the machine's headline and age from `watcher.latest`.

### 11.5 Printers (the password is entered once)

```
┌ Printer ──────────────────────────────────────────────────────────────────────────────┐
│ Name        [Printer 1          ]                                                     │
│ IP address  [192.0.2.10         ]  The printer's fixed address.                        │
│ Port        [80   ]                80 for a real printer.                              │
│ UID         [1109 ]                Checked on every poll and Send.                     │
│ Password    [                   ]  A password is saved. Type a new one only to change it. │  password_entry, password_status
│             Stripes keeps only a scrambled token made from the password, never the       │
│             password itself.                                                             │
│ [x] Active                                                                               │
│ Used by: Line 1 Bagger 1 · Printing · checked 3 s ago                                    │
└──────────────────────────────────────────────────────────────────────────────────────────┘
```

1. `password_entry` is a `ttk.Entry` with `show="•"` and **no** `textvariable`, with the hint "Stripes
   keeps only a scrambled token made from the password, never the password itself." `password_status`:
   - `token == ""`: "No password saved yet. Stripes can't check this printer until one is."
   - `hx_protocol.unscramble(token, release_key())` succeeds: "A password is saved. Type a new one only
     to change it."
   - it raises `AuthFailed`: "The saved password was made by a different Stripes build. Type it
     again."
2. On Save, before anything else: `pw = password_entry.get()`; when not empty, `record.token =
   hx_protocol.scramble(hx_protocol.make_token(pw), hx_protocol.release_key())`, then
   `password_entry.delete(0, "end")` and `del pw`. Only the scrambled token goes into `working` and
   the record (protocol.md §3.1 rule 3); a failed save can be retried without typing it again. The
   password is never put in a `Msg`, a log, a print, a Tk variable, the clipboard or a file. The admin
   log shows "(set)" (storage.md §13.4 step 7).
3. Changing the address or UID keeps the token.
4. `printer_problems(printer, data)`, in order: "Give the printer a name." when the name is empty
   (at most 40 characters); "Another printer already has that name." (names unique ignoring case);
   "Type the printer's IP address, for example 192.0.2.10." unless `ipaddress.IPv4Address(host)`
   parses; "{name} already has this address." when another active printer has the same host and port;
   "The port must be a number from 1 to 65535."; "The UID must be a whole number from 1 to
   4294967295." (the range protocol.md §5.2 accepts for HXUI). The two name texts are the existing
   Zebra ones.
5. "Used by:" the active machine with this `printer_id`, with its headline and age from the board, or
   "Not used by any machine yet." Stripes checks a printer through its machine, so the admin sees
   whether the password works on that machine's card ("Stripes can't log in to this printer") within
   a few seconds of saving. The admin screens never send requests to a printer themselves.

### 11.6 Products and films

```
┌ Products and films ───────────────────────────────────────────────────────────────────┐
│ [New product] [Add new version]   [ ] Show not active                                  │
│ ┌ tree ──────────────────────────────────────┐ ┌ editor ─────────────────────────────┐│
│ │ Example Co 2lb Onion   Example Co   Yes     │ │ ProductEditor, or FilmEditor (§11.7)││
│ │   EXCO1 · Rev C        Active        4      │ │                                      ││
│ │   EXCO2 · Rev D        In development 1     │ │                                      ││
│ └─────────────────────────────────────────────┘ └──────────────────────────────────────┘│
└─────────────────────────────────────────────────────────────────────────────────────────┘
```

1. Selecting a product shows `ProductEditor`: `name` (Entry), `customer` (Entry), `active`
   (Checkbutton) and a read-only list of its films.
2. Selecting a film shows `FilmEditor`.
3. `new_product_button` "New product". `new_version_button` "Add new version" (a product or one of its
   films selected) creates `new_film(product_id)`: a new, blank film (plan), status "In development",
   shown in `FilmEditor` with the note "A new artwork revision is a new film. It starts blank and
   needs its own test print."
4. `product_problems(product, data)`: "Give the product a name." (empty, or over 80 characters);
   "Another product already has that name." (among active products, ignoring case); the customer at
   most 80 characters ("The customer name is too long.").

### 11.7 The film editor (all film fields)

`FilmEditor` is a `Scroller` with these sections, in this order. Values are bag text where marked.

```
┌ Film ─────────────────────────────────────────────────────────────────────────────────┐
│ Product: Example Co 2lb Onion                                                          │
│ Code [EXCO1 ]  Up to 6 capital letters or digits, used in the printer's message name.  │
│ Artwork revision [Rev C ]   Status [Active v]   Settings revision 4                     │
│ ── Bag ──                                                                               │
│ (•) Back film: the code goes on the back   ( ) Front film: the code goes on the front   │
│ Paper or plastic [Plastic v]   Ink colour [Black v]                                     │
│ ── Placement ──                                                                         │
│ Film length (mm) [610 ]  Print position (mm) [150 ]  Delay (ms) [480 ]  Print length (mm) [   ] │
│ ── Print settings ──                                                                    │
│ Font [2: Arial v]  Size [4 ]  Resolution [Ultra fine v]  Bold [1 ]  Width [1 ]          │
│ ── Origin ──                                                                            │
│ [ ] The origin is pre-printed on the film    Pre-printed origin [        ]  {preview}   │
│ ── Print texts ──                                                                       │
│ [x] Packed on + origin (PKO) · Active      [ ] Packed on + origin + best-by (PKB) · Active │
│ Default [Packed on + origin v]                                                          │
│ ── Files and photos ──                                                                  │
│ {front photo 160px} {back photo 160px}                                                  │
│ ┌ files_tree: Kind, Name, Size, Added by, Added ──────────────────────────────────────┐ │
│ └──────────────────────────────────────────────────────────────────────────────────────┘ │
│ Add as [Front photo v] [Add file...]  [Open]  [Remove from film]                        │
│ ── Machine adjustments ──                                                               │
│ ┌ adjust_tree: Machine, Placement, Adjustment, Set by, Set at ─────────────────────────┐ │
│ └──────────────────────────────────────────────────────────────────────────────────────┘ │
│ [Edit adjustment...]   Ignored during a test print.                                     │
│ [Test print...]                                              [Revert]   [ Save ]        │
└─────────────────────────────────────────────────────────────────────────────────────────┘
```

#### 11.7.1 Fields

| Section | Field | Widget | Hint (Muted) |
|---|---|---|---|
| Film | `product_id` | read-only "Product: {name}" | |
| | `code` | Entry (width 8) | "Up to 6 capital letters or digits, used in the printer's message name. A new film needs a new code." |
| | `artwork_revision` | Entry | |
| | `status` | `ChoiceBox`: `development` "In development", `active` "Active", `inactive` "Not active" | "Only Active films are offered at Send." |
| | `revision` | read-only "Settings revision {n}" | "Goes up on every save." |
| Bag | `side` | 2 Radiobuttons: `back` "Back film: the code goes on the back", `front` "Front film: the code goes on the front" | shown to senders as an instruction |
| | `cartridge` | `ChoiceBox`: `paper` "Paper (Boxtron)", `plastic` "Plastic" | "Must match the line's cartridge type." |
| | `ink_colour` | `ChoiceBox`: `white` "White", `black` "Black" | "Checked against the ink the printer reports." |
| Placement | `length_mm` | Entry "Film length (mm)" | "The bag's cut-off length, from the spec sheet. Needed for encoder machines." |
| | `position_mm` | Entry "Print position (mm)" | "From the bag's bottom edge, the edge that feeds in first. Needed for encoder machines. Found with the test print." |
| | `delay_ms` | Entry "Delay (ms)" | "Time from the trigger to the print. Needed for delay machines. Found with the test print." |
| | `print_length_mm` | Entry "Print length (mm)" | "How long the code is along the film, for the warning when a print would still be running as the film stops. Leave empty if unknown." (OPEN) |
| Print settings | `font` | `RecordBox` of `company.standard_fonts` shown "{index}: {name}" (data) | "One font and size for the whole print text." |
| | `size` | Spinbox 1–10 | |
| | `resolution` | `ChoiceBox`: 0 "Ultra fine", 1 "Fine", 2 "Normal", 3 "Draft" | |
| | `bold`, `width` | Spinbox 1–10 each | |
| Origin | `origin_preprinted` | Checkbutton "The origin is pre-printed on the film" | ticked: "Only print texts without an origin item can be used."; unticked: "Only print texts with an origin item can be used." |
| | `preprinted_origin` | Entry (bag text), enabled only when ticked, and a `BagText` (13 bold) with `origin_preview(film, [])` | "Shown at Send, on the floor board and in the log." |
| Print texts | `print_text_ids` | a Checkbutton per `print_text_choices(film, data)`: "{name} ({code}) · {status}" | ticking appends the id at the end; unticking removes it |
| | `default_print_text_id` | `RecordBox` of the ticked print texts | "Pre-filled at Send." |
| Files | `files` | §11.7.3 | "Originals stay on the server. Each PC keeps small previews of the photos." |

1. Changing `origin_preprinted` unticks every print text that no longer passes `origin_rule_ok` (and
   clears the default if it was one of them) and shows "Print texts that don't suit this setting were
   unticked." (`Warn.TLabel`).
2. Checks before saving: `hx_message.validate_film(film, data)` blocks. Warnings that don't block, for
   an Active film: "Delay machines can't send this film until it has a delay." when `delay_ms` is None;
   "Encoder machines can't send this film until it has a film length and a print position." when
   either is None.
3. A film whose fields came from a test print is saved like any edit; its status doesn't change (plan).

#### 11.7.2 Photos

`thumbs["front_photo"]` and `thumbs["back_photo"]`: the last `FileRef` of each kind, shown 160 px tall
with `photo_from_png` of `data.preview_bytes(film_id, sha256)` (loaded with `bg.run`), or "No photo".
Clicking one opens `PhotoWindow`. Previews come from the PC's cache when it has them (storage.md §13.5);
the originals stay on the share.

#### 11.7.3 Files

`files_tree` columns "Kind" (translated kind), "Name" (`FileRef.name`), "Size" (`f"{size / 1e6:.1f} MB"`),
"Added by", "Added" (`when`). `add_kind_box` (`ChoiceBox` of the five `FILE_KINDS`: "Front photo",
"Back photo", "Artwork", "Spec sheet", "Other file") preselects the first of front photo, back photo,
artwork that the film doesn't have yet, else "Other file".

Adding a file (`add_file_button` "Add file..."):

1. `path = filedialog.askopenfilename(parent=self, title=tr("Choose a file to add"),
   filetypes=[(tr("Pictures"), "*.png *.jpg *.jpeg *.bmp *.gif *.tif *.tiff *.webp"), (tr("PDF files"),
   "*.pdf"), (tr("All files"), "*.*")])`; nothing chosen: stop.
2. `bg.run(work)`: `ref = data.save_file(film_id, kind, path)`; `png = png_preview(path)`; when `png`:
   `data.save_preview(film_id, ref.sha256, png)`; return `(ref, png)`.
3. Done: for `front_photo` and `back_photo`, earlier `FileRef`s of that kind are removed from
   `working.files` (the originals stay on the share); `ref` is appended; `dirty = True`; the
   thumbnail shows `png`; the note "The file is added. Save the film to keep it." Errors (e.g. "The
   file is too big (at most {mb} MB).") show with `text(e.msg)`.
4. A film that was never saved can take files (`save_file` makes its folder); it still has to be saved.

`png_preview(path)` (worker thread): Pillow `Image.open(path)`, `ImageOps.exif_transpose`,
`convert("RGB")`, `thumbnail((480, 480), Image.LANCZOS)`, saved as PNG with `optimize=True`. Over
512,000 bytes: again at 360, then 240. Still over, or any Pillow error (`UnidentifiedImageError`,
`OSError`, `Image.DecompressionBombError`): None. Previews only for formats Pillow already reads;
anything else opens in the PC's default viewer (plan).

`open_file_button` "Open": `bg.run(lambda: data.share_path("templates/" + ref.path))`, then on another
`bg.run` `os.startfile(path)` on Windows or `webbrowser.open("file://" + path)` elsewhere. No path:
"Stripes can't reach the server, so the file can't be opened now."

`remove_file_button` "Remove from film": removes the `FileRef` from `working.files` (`dirty`); the
original stays on the share (storage.md §4.9).

#### 11.7.4 Machine adjustments

`adjust_tree` lists the active machines of every active line whose `cartridge == film.cartridge`:
"Machine", "Placement" ("Encoder"/"Delay"), "Adjustment" (`+20 ms`, `-3 mm` or `0`, from
`data.adjustment(machine_id, film_id)`), "Set by", "Set at". `adjust_button` "Edit adjustment..." opens
`AdjustmentDialog` (§11.9) for the selected machine and this film; disabled until the film is saved.

#### 11.7.5 Test print

`test_button` "Test print..." calls `nav.open_test_print(film_id=film.id)`; disabled while the film
isn't saved or is `dirty` ("Save the film first."). Any status may be tested (plan).

### 11.8 The print-text designer

`PrintTextEditor` (in the Print texts page):

```
┌ Print text ───────────────────────────────────────────────────────────────────────────┐
│ Name [Packed on + origin  ]  Code [PKO]  Status [Active v]  Revision 7                 │
│ ┌ items_tree ─────────────────────────────────────────────────────────────────────────┐│
│ │ #  Item             Text before    Data            Text after   Placement   Gap      ││
│ │ 1  Packed-on date   "Packed on "   MM/DD/YY        ""           –           0        ││
│ │ 2  Country of origin               [countries]                  Below       0        ││
│ │ 3  Lot              "Lot "         [lot]           ""           Right       4        ││
│ └─────────────────────────────────────────────────────────────────────────────────────┘│
│ [Fixed text v] [Add item]  [Move up] [Move down] [Remove item]                          │
│ ┌ item editor (the selected item) ────────────────────────────────────────────────────┐│
│ │ Text before [Lot      ]   Text after [         ]                                     ││
│ │ (•) Right of the previous item   ( ) Below the previous item     Gap [4 ]            ││
│ └─────────────────────────────────────────────────────────────────────────────────────┘│
│ Preview with the longest values                                                         │
│ {Packed on 10/29/26                                                }  mono BagText      │
│ {Product of USA, Canada, and MexicoLot 88888888                    }                    │
│ Longest line: 45 characters. Approximate: the test print shows exactly how it prints.   │
│ Films that may use this print text                                                      │
│ [x] Example Co 2lb Onion · EXCO1 · Rev C   Active   Default for this film                │
│ [ ] Example Co 2lb Onion · EXCO2 · Rev D   In development                                │
│ Only films whose origin isn't pre-printed are listed, because this print text has an    │
│ origin item.                                                                             │
│                                                              [Revert]   [ Save ]        │
└─────────────────────────────────────────────────────────────────────────────────────────┘
```

#### 11.8.1 Header

`name` (Entry), `code` (Entry, width 5; "Up to 3 capital letters or digits, used in the printer's
message name."), `status` (`ChoiceBox` as for films), revision (read-only "Revision {n}"; "Goes up on
every save. The send log records which one printed.").

#### 11.8.2 Items

1. `items_tree` columns "#", "Item", "Text before", "Data", "Text after", "Placement", "Gap". Item
   names: `fixed` "Fixed text", `packed_date` "Packed-on date", `best_by_date` "Best-by date", `lot`
   "Lot", `origin` "Country of origin", `free_text` "Free text", `machine_code` "Machine code",
   `line_code` "Line code". Text before and after are quoted (§2.3 rule 6). Data: `fixed` the quoted
   text; dates the format key (best-by adds " +{offset} {unit}" with the unit translated); `lot`
   "[lot]"; `origin` "[countries]"; `free_text` "[" + label + "]"; codes "[machine code]", "[line
   code]". Placement: "–" for the first item, else "Right" or "Below". The tree is refilled on
   `retranslate()`.
2. `add_kind_box` (`ChoiceBox` of the item names) and `add_item_button` "Add item" append
   `PrintTextItem(id=<the first free "i1", "i2", ...>, kind=..., placement="right")`, with
   `date_format="MM/DD/YY"` for dates, `offset=1` for best-by, `max_len=min(10, company.free_text_max)`
   and `label=""` for free text. An origin item can be added only when there is none ("A print text has
   at most one origin item.").
3. `up_button` "Move up", `down_button` "Move down", `remove_item_button` "Remove item" act on the
   selected item (removing an item from a print text is an ordinary edit, not a record delete).
4. The item editor shows, by kind (every field is a widget in `item_fields`):

   | Kind | Fields |
   |---|---|
   | Fixed text | Text (Entry, bag text) |
   | Packed-on date | Text before, Date format, Text after |
   | Best-by date | Text before, Date format, "Offset" Spinbox 1–`OFFSET_MAX[unit]` with unit `ChoiceBox` (`days` "days", `months` "months", `years` "years"), Text after |
   | Lot | Text before, Text after |
   | Country of origin | no fields; the note "Always prints "Product of" and the countries chosen at Send. It has no text before or after." |
   | Free text | "Question for the sender" (Entry, data), "Most characters" (Spinbox 1–`company.free_text_max`), "Required" (Checkbutton), Text before, Text after |
   | Machine code, Line code | Text before, Text after |

   Every item but the first also has Placement (`Radiobutton`s "Right of the previous item" / "Below the
   previous item") and "Gap" (Spinbox 0–`GAP_MAX`, hint "Extra space before the item, in printer canvas
   units."). Date format is a read-only `ttk.Combobox` of the `DATE_FORMATS` keys (not translated:
   they are codes) with the example "prints {BagText of render_date(DATE_FORMATS[key], today)}".
5. Text before, text after and fixed text are bag text: Entries, never translated, with spaces kept.
   Origin words are refused when the print text is saved (`validate_print_text`), and the problems say
   which item.

#### 11.8.3 Checks before saving

`validate_print_text(pt, company)` blocks, then `membership_saves` (§11.8.5). The designer also shows
those problems live, after each change, in `problems_label`.

#### 11.8.4 The longest-value preview

The designer previews the longest possible value (plan), so a long value like "USA, Canada, and
Mexico" can be seen pushing the next item along. `preview` is a `BagText` (mono family, size 12),
holding `"\n".join(preview_lines(pt, longest_values(pt, company, today)))`, with `lengths_label` "Longest
line: {n} characters." and the note "Approximate: the test print shows exactly how it prints."

`longest_values(pt, company, today) -> dict[item_id, str]`:

| Kind | Value |
|---|---|
| `fixed` | `item.text` |
| `packed_date` | `before + render_date(DATE_FORMATS[fmt], today) + after` |
| `best_by_date` | `before + render_date(DATE_FORMATS[fmt], today, add_<unit>=offset) + after` |
| `lot` | `before + "8" * company.lot_max_digits + after` |
| `origin` | `origin_phrase([c.name for c in company.countries if c.active])`, `""` with no active country |
| `free_text` | `before + "W" * item.max_len + after` |
| `machine_code`, `line_code` | `before + "WWW" + after` |

A value that can't be worked out (an unknown date format) is `"?"`; the function never raises.

`preview_lines(pt, values)`: start a new line for the first item and for every item placed `"below"`;
otherwise append to the current line, after a space when `gap > 0` (as send.md §3.5 renders one line).
Returns the lines.

#### 11.8.5 Films allowed, from the print text side

The film's `print_text_ids` is the only copy of the list (architecture §5.5); the print text editor
edits it from the other side.

1. `film_vars[film_id]`: a Checkbutton per `film_choices(pt, data)`: "{product} · {code} · {revision}"
   (data), then the film's status, then "Default for this film" when `film.default_print_text_id ==
   pt.id`. Ticked when `pt.id in film.print_text_ids`.
2. The note: "Only films whose origin isn't pre-printed are listed, because this print text has an
   origin item." or "Only films with a pre-printed origin are listed, because this print text has no
   origin item." (A print text with two origin items lists none; validation already refuses it.)
3. `membership_saves(pt, wanted, data)` (pure): with `data2 = dataclasses.replace(data,
   print_texts={**data.print_texts, pt.id: pt})`, for each film of `film_choices(pt, data2)` whose
   membership changes, a copy with `pt.id` appended to or removed from `print_text_ids`. Removing the
   film's default gives the problem "{film} uses this print text as its default. Choose another default
   in that film first." Each copy must pass `validate_film(copy, data2)`; its problems are prefixed
   "{film}: {problem}". Returns `(films to save, problems)`.
4. The UI adds a problem "Save or revert your changes to {film} first." for a changed film that is
   `dirty` in the film editor.
5. Save: any problem → nothing is saved. Otherwise one `bg.run` saves the print text
   (`expected_revision` as usual), then each film in turn with `expected_revision=film.revision`. A
   film that fails doesn't stop the others; the result shows "Saved {name}." and, if any film failed,
   "The print text was saved, but these films weren't:" with "{film}: {problem}" lines.
6. From the film side (§11.7.1) the list is edited directly and saved with the film.

### 11.9 Machine + film adjustments

`AdjustmentsPage`: `machine_filter` and `film_filter` (`RecordBox`es with `none_text` "All machines" /
"All films") filter the list; `new_button` "New adjustment..." and double-click open the dialog.

```
┌ Machine + film adjustment ────────────────────────────────────────────┐
│ Machine  [Line 1 Bagger 1 v]   Delay machine                           │  machine_box
│ Film     [EXCO1 · Rev C   v]                                           │  film_box
│ Delay adjustment (ms) [+20   ]   Can be negative. 0 means none.         │  value_entry (or "Position adjustment (mm)")
│ 480 + 20 = 500 ms = 500000 µs                                          │  working_label
│ Ignored during a test print.                                           │
│ Set by EXAMPLE\jsmith at 2026-10-29 14:00                               │
│                                              [Cancel]   [ Save ]       │
└────────────────────────────────────────────────────────────────────────┘
```

1. `AdjustmentDialog(master, services, *, machine_id, film_id)`. The value field is "Position
   adjustment (mm)" for encoder machines and "Delay adjustment (ms)" for delay machines (plan); the
   other value of an existing record is kept unchanged.
2. `working_label`: `hx_message.explain_position(machine, film, <the adjustment with the typed
   value>).text` when it doesn't raise; empty otherwise.
3. `adjustment_problems(adj, data)`: "Choose a machine and a film." unless both exist; "The position adjustment must be
   between -1000 and 1000 mm."; "The delay adjustment must be between -10000 and 10000 ms."
4. Save: `data.save(MachineFilmAdjustment(id=adjustment_id(m, f), machine_id=m, film_id=f,
   position_mm=..., delay_ms=...), expected_revision=<existing.revision, or None when there is no
   record>)`. Who set it and when are the record's `updated_by`/`updated_at`, shown as "Set by {user} at
   {time}".

## 12. The test print window

`nav.open_test_print(film_id=..., machine_id="", kind="test")` (administrators only) opens one
`TestPrintWindow` per film, title "Test print". `kind` must be `"test"` (`ValueError` otherwise):
calibration prints run from `CalibrationWindow` (§13), which uses the same `TestRunPanel`. The heading
is "Test print: {film} ({product})" (`film` = code · artwork revision, `product` = the product's name).

### 12.1 Layout

```
┌ Test print ─────────────────────────────────────────────────────────────────────────────┐
│ Test print: EXCO1 · Rev C (Example Co 2lb Onion)                          (Hx.Head)    │
│ Machine [Line 1 Bagger 1 v]  Delay machine · Printer off                               │  machine_box
│ [x] Line 1 Bagger 1 is running empty film, with no product.                            │  empty_film_var
│ ── What to vary ──                                                                      │
│ Delay (ms): from [440 ] to [520 ] in steps of [20 ]       (encoder: Print position (mm)) │  start_entry, stop_entry, step_entry
│ Bold:  [ ]1 [ ]2 [ ]3 [ ]4 [ ]5 [ ]6 [ ]7 [ ]8 [ ]9 [ ]10   None ticked: the film's.       │  bold_vars
│ Width: [ ]1 ... [ ]10                                                                   │  width_vars
│ Resolution: [ ]Ultra fine [ ]Fine [ ]Normal [ ]Draft                                    │  resolution_vars
│ Fonts side by side: [2: Arial v][3 ]  [2: Arial v][4 ]  [Add font] [Remove font]        │  font_rows
│ ──                                                                                      │
│ 5 steps · about 25 bags. Each step runs for 5 bags, counted by the printer.             │  estimate_label
│ (Bad) problems                                                                          │  problems_label
│                                                       [Close]   [ Start test print ]    │  start_button
│ ┌ TestRunPanel (when running or finished) ──────────────────────────────────────────┐   │
│ │ Step 3 of 5   {D480 | B1}                                                          │   │  step_label, label_text
│ │ Printing: 2 of 5 bags                                                              │   │  status_label
│ │ {Picture 768}                                                                      │   │  picture
│ │                                                                    [Stop test]     │   │  stop_button
│ └────────────────────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────────────────┘
```

### 12.2 Choosing

1. `machine_box`: active machines whose printer is set up, by name; preselects `machine_id` when given.
   Next to it: "Delay machine" or "Encoder machine", and the machine's headline from the board.
2. `empty_film_var` "{machine} is running empty film, with no product." must be ticked (plan: the admin
   confirms it first).
3. What varies follows the machine (plan): encoder machines vary "Print position (mm)", delay machines
   "Delay (ms)". The entries start at the film's value minus two steps, plus two steps, step 10 mm or
   20 ms; empty when the film has no value. Bold, width and resolution: tick the extra values to try;
   none ticked keeps the film's. Fonts side by side: up to `MAX_FONT_SAMPLES` rows of (font, size);
   none keeps the film's font and size. Print speed isn't varied (a machine setting).
4. After every change: `ranges = TestRanges(vary="position" | "delay", start, stop, step, bold=[...],
   width=[...], resolution=[...], fonts=[...])`, then `plan = hx_message.plan_test(machine_id, film_id,
   ranges, data, identity, kind="test")` on the Tk thread (pure). `PlanError` → `problems_label`.
   Otherwise `estimate_label`: "{steps} steps · about {bags} bags." (`len(plan.steps)`,
   `hx_message.test_bag_estimate(plan)`), then "Each step runs for {n} bags, counted by the printer."
   (`plan.bags_per_step`), or, when `plan.timed`, "Each step runs for about {seconds} s, because no
   counter counts bags." (`seconds = round(machine.seconds_per_bag * plan.bags_per_step)`).
5. `start_button` "Start test print" is enabled when the plan has no problems, `empty_film_var` is
   ticked, `roles.admin`, and `services.store.available` (a test needs the share lock held, send.md;
   otherwise "The server can't be reached, so a test print can't take the printer's lock.").

### 12.3 Running

1. `ask(Msg(N_("Start the test print on {machine}? It takes the printer for the whole test, about {bags}
   bags, and ends with printing off and slots 9 and 10 empty.")), yes=N_("Start"), no=N_("Go back"))`.
2. `run = services.test_run(plan)`; `cancel_token = CancelToken()`; `mark_running(f"test:{run_id}",
   Msg(N_("A test print on {machine} is still running."), ...))`; `start_thread(services,
   f"hx-test-{plan.run_id}", plan.run_id, lambda: run.run(services.bus.post, cancel=cancel_token))`.
   The choosing widgets are disabled.
3. While it runs the machine shows "TEST by {user}, step {step} of {of}" on every monitor and the floor
   board (states.md; nothing for the UI to do here).

### 12.4 TestRunPanel

Shows `TestEvent`s with this `run_id`:

| `status` | `status_label` |
|---|---|
| `building` | "Building and checking the next message..." |
| `printing` | "Printing: {bags} of {per} bags" (`per` = `bags_per_step`; "Printing..." when `bags` is None); send.md posts it after each switch and again whenever the step's count changes, so the count moves during a step |
| `ok` | "Step done." |
| `failed` | `text(event.msg)` (`Bad.TLabel`) |
| `stopping` | "Stopping: printing off, then emptying slots 9 and 10." (Stop test takes effect after the current request, architecture §7.5 rule 3) |
| `cleared` | "Printing is off and slots 9 and 10 are empty." |

`step_label` "Step {step} of {of}"; `label_text` (`BagText` 14 bold) the step's label from
`plan.steps[step_index - 1].label` ("P150 | B1", "D480 | B1": the number the admin types into the
film); `picture` (`PictureView`, width 768) the event's `design.preview_bmp` when it has one.
`stop_button` "Stop test": `cancel_token.cancel()`, disabled, `status_label` shows the stopping text.

### 12.5 Finished

On `TestFinished` (or `WorkFailed`) with this run id: `mark_done`; then:

1. "Finished: {done} of {of} steps." when `result.ok`, else "Stopped after {done} of {of} steps:
   {problem}".
2. `result.cleared`: "Printing is off and slots 9 and 10 are empty. {machine} needs a normal Send
   before production." (`Ok.TLabel`); not cleared: "Stripes couldn't empty slots 9 and 10. Check the
   printer, and send to it before production." (`Bad.TLabel`).
3. `steps_tree` lists every step: "Step", "Mark" (the step's label, bag text), "Position or delay", "Bold", "Width",
   "Resolution", "Fonts", so the admin can match each mark on the film to its values.
4. `film_button` "Open the film": `nav.open_admin(page="products", record_id=film_id)`, with the hint
   "Type the chosen values into the film. That's a normal edit; the film's status doesn't change."
5. **Printing not confirmed off** (`result.printing_off` is False): the window turns into the full-screen
   red page of §8.1 item 7 (`attributes("-fullscreen", True)`, `-topmost`, `RED_BG`), with "Stop it at
   the printer now", "Stripes couldn't stop printing on {machines}." (the test's machine), "Press
   Print On/Off on that printer now. Its card shows "Stop it at the printer" until Stripes reads that
   printing is off.", `text(result.msg)`, and `end_try_button` "Try again" and Close. Try again runs
   `run.end_again(services.bus.post)` (send.md §15.5) on `start_thread(services,
   f"hx-test-{run_id}", run_id, ...)` with `mark_running`; the administrator who ran the test may use it
   without being a sender (Stop, on the machine card, needs the sender group). Its `TestFinished`
   replaces the result: `printing_off` True returns to the normal finished page; False keeps the red
   page. Close is always allowed on this page.

`close()` while running: `ask(Msg(N_("Stop the test print?")), yes=N_("Stop test"), no=N_("Keep it
running"))`; yes cancels, and the window stays until `TestFinished` (the end procedure always runs).
Otherwise it closes.

Test attributes: `machine_box`, `empty_film_var`, `start_entry`, `stop_entry`, `step_entry`,
`bold_vars`, `width_vars`, `resolution_vars`, `font_rows`, `estimate_label`, `problems_label`,
`start_button`, `plan`, `cancel_token`, `panel` (`TestRunPanel`: `step_label`, `label_text`,
`status_label`, `picture`, `stop_button`), `steps_tree`, `film_button`, `end_try_button`.

## 13. The calibration window (OPEN)

How encoder machines are calibrated is open while the encoder is worked out (plan; architecture §11).
This window is the interface and the safe default; it follows send.md §16 exactly, and the procedure is
to be confirmed on the test unit. `CalibrationWindow(master, services, *, machine_id)`
(administrators, from the machine page), title "Calibrate {machine}". A delay machine gets
`tell(Msg(N_("Delay machines aren't calibrated.")))` and no window.

```
┌ Calibrate Line 1 Bagger 1 ──────────────────────────────────────────────────────────────┐
│ Calibrate once, at installation, with empty film. This procedure is still being          │
│ confirmed on the test unit.                                              (Muted)        │
│ Film loaded for calibration [EXCO1 · Rev C (610 mm) v]                                  │  film_box
│ [x] Line 1 Bagger 1 is running empty film, with no product.                             │  empty_film_var
│ ── 1. Rough head distance ──                                                             │
│ Rough head distance by tape measure (mm) [400 ]  [Save rough distance]                   │  rough_var, save_rough_button
│   From the print head back along the film path to where a bag's bottom edge sits when    │
│   the film stops. It can be longer than one bag.                                         │
│ Saved: 400 mm.                                                                            │  rough_status
│ ── 2. Distance scale ──                                                                   │
│ Stripes prints two marks 300 mm apart, at 365 mm and 55 mm after the trigger.            │  spacing_text
│ [Print the two marks]                                                                    │  print_spacing_button
│ Mark A from the bag's bottom edge (mm) [167 ]  Mark B (mm) [467.5 ]                       │  mark_a_var, mark_b_var
│ Gap: 300.5 mm. The gap is within 2 mm: the wheel size is right.                           │  spacing_result
│ ── 3. Head distance ──                                                                   │
│ Stripes prints one mark 515 mm after the trigger.                                        │  head_text
│ [Print the mark]                                                                         │  print_head_button
│ Measured from the bag's bottom edge (mm) [317 ]                                          │  head_measured_var
│ Head distance: 412 mm                                                                    │  head_result
│ ── ──                                                                                    │
│ Last spacing check: 300.5 mm by EXAMPLE\jsmith at 2026-10-29 13:00.                      │
│ Last head distance: 412 mm by EXAMPLE\jsmith at 2026-10-29 14:00.                        │
│ TestRunPanel (while printing)                                                            │
│                                                        [Close]   [ Save calibration ]   │  save_button
└──────────────────────────────────────────────────────────────────────────────────────────┘
```

1. At the top, in `Muted.TLabel`: "Calibrate once, at installation, with empty film. This procedure is
   still being confirmed on the test unit." (remove the second sentence once confirmed). `film_box`
   ("Film loaded for calibration"): films of any status that have `length_mm`, by product and code,
   shown "{code} · {revision} ({length} mm)" (data). The film's length `L` decides where the marks
   land.
2. **Rough head distance first.** send.md §16.2 plans neither print without
   `Calibration.rough_head_distance_mm`, so this is step 1. `save_rough_button` "Save rough distance":
   `cal` = the machine's `Calibration` from `data.current.calibrations` (a copy), or a new
   `Calibration(id=machine_id, machine_id=machine_id)`; `cal.rough_head_distance_mm = <rough_var>`;
   `bg.run(lambda: data.save(cal, expected_revision=<cal's revision, None when new>))`. The print buttons
   stay disabled ("Save the rough distance first.") until a `DataUpdate` arrives whose
   `calibrations[machine_id].rough_head_distance_mm` equals the saved value; `rough_status` then reads
   "Saved: {mm} mm." Changing the rough value disables them again until it is saved.
3. **The plans.** With the rough distance in `data.current`, the window plans both prints on the Tk
   thread (pure): `spacing_plan = plan_test(machine_id, film_id, TestRanges(vary="position", start=0.0,
   stop=0.0, step=1.0), data, identity, kind="spacing")` and `head_plan = plan_test(...,
   kind="head_distance")`. The `TestRanges` is a placeholder: send.md §16.2 ignores it for these kinds.
   Both work on a machine that has never been calibrated (`head_distance_mm` None, send.md §15.2 item
   6). A `PlanError` shows its problems under the step and disables its button. The positions shown are
   the ones that will be sent, read from the plans: `a = spacing_plan.steps[0].position_mm`, `b =
   spacing_plan.steps[1].position_mm` and `h = head_plan.steps[0].position_mm` (whole mm in `[0, L)`,
   each equal to `test_step_plan(plan.base, step).settings.position_mm`, the DLYMM sent; send.md §16.2).
   `spacing_text` reads "Stripes prints two marks {mm} mm apart, at {a} mm and {b} mm after the
   trigger." and `head_text` "Stripes prints one mark {h} mm after the trigger." Pressing a print
   button keeps what that run prints: `spacing_run` = (`spacing_plan`, its `film_id`, `L`, the saved
   rough `R`) or `head_run` = (`head_plan`, its `film_id`, `L`, `R`, `sent = int(head_plan.steps[0].
   position_mm)`). When the saved rough distance in `data.current` or `film_box`'s film no longer
   equals the one kept, the window clears `mark_a_var`, `mark_b_var`, `head_measured_var`,
   `spacing_result`, `head_result`, `spacing_run` and `head_run`, and the marks must be printed again.
4. **Distance scale.** `print_spacing_button` "Print the two marks" runs `spacing_plan` in the
   `TestRunPanel` exactly as in §12.3–§12.5. `mark_a_var` ("Mark A from the bag's bottom edge (mm)") and
   `mark_b_var` ("Mark B (mm)") are disabled, with the hint "Print the marks first, then measure them.",
   until that run's `TestFinished` arrives with `steps_done == 2`. The admin then measures each mark from
   the bag's bottom edge, as send.md §16.1 asks. `gap = hx_message.spacing_gap_mm(a_measured,
   b_measured, L)` with the `L` kept in `spacing_run`; `spacing_result` reads "Gap: {gap} mm." then,
   within `hx_message.SPACING_TOLERANCE_MM` of `spacing_sent_mm` (300), "The gap is within {mm} mm: the
   wheel size is right." (`Ok.TLabel`); otherwise, when `machine.encoder_wheel_mm` is set and `wheel =
   hx_message.wheel_suggestion_mm(machine.encoder_wheel_mm, spacing_sent_mm, gap)` isn't None (a whole
   mm, as ENWHD takes), "Correct the wheel size on the printer to {wheel} mm and set Expected wheel size
   on this machine's page to {wheel} mm, then print the marks again."; otherwise "The gap is {measured}
   mm instead of {sent} mm. Correct the wheel size on the printer and set the same value as Expected
   wheel size on this machine's page, then print the marks again." Stripes never changes the wheel
   size on the printer itself (plan); the machine record must match it, or the next print fails the
   machine check (send.md §16.1 item 2).
5. **Head distance.** `print_head_button` "Print the mark" runs `head_plan` the same way.
   `head_measured_var` is disabled, with the hint "Print the mark first, then measure it.", until that
   run's `TestFinished` arrives with `steps_done >= 1`. After the admin types where the mark landed,
   `head_result` shows "Head distance: {mm} mm" with `mm = hx_message.head_distance_from_print(sent,
   measured, L, rough)`, where `sent`, `L` and `rough` are those kept in `head_run` (the head print that
   ran, not the current plan and not a value the admin typed). A `PlanError` ("The tape measurement is
   too far ...") shows under the step.
6. **Save calibration** (`save_button`): the machine's `Calibration` from `data.current.calibrations`
   (the one step 2 saved), with the spacing fields set when `spacing_result` holds a gap from
   `spacing_run` (`spacing_sent_mm`, `spacing_measured_mm = gap`, `spacing_by = identity.user`,
   `spacing_at = clock.time()`) and the head fields only while `head_run` is set and `head_result`
   holds a value (`head_print_sent_mm` = the `sent` of `head_run`, `head_print_measured_mm`,
   `head_distance_mm`, `head_by`, `head_at`); `data.save(cal, expected_revision=cal.revision)`. Storage
   copies the head distance into the machine (storage.md §13.4 step 8). Done: "Saved. The head distance
   of {machine} is now {mm} mm."
7. History lines from the machine's `Calibration`: "Last spacing check: {mm} mm by {user} at {time}."
   and "Last head distance: {mm} mm by {user} at {time}.", each shown only when recorded.
8. Each print takes the printer's lock for its run (purpose `calibration`), shows "TEST by ..." on the
   board, and ends with printing off and both slots empty, like any test print.

Test attributes: `film_box`, `empty_film_var`, `rough_var`, `save_rough_button`, `rough_status`,
`spacing_plan`, `head_plan`, `spacing_run`, `head_run`, `spacing_text`, `print_spacing_button`,
`mark_a_var`, `mark_b_var`, `spacing_result`, `head_text`, `print_head_button`, `head_measured_var`,
`head_result`, `save_button`, `panel`.

## 14. Per-user settings

`HxSettings` lives in `%APPDATA%\Stripes\v2\maplejet.json` (storage.md §7.1), so it roams with the
user; the floor-board PC's user sets it once there.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `floor_board_at_startup` | bool | False | open the floor board whenever Stripes starts, including at sign-in from the shortcut IT deploys |
| `floor_board_screen` | int | 0 | index into `hx_win.monitors()`; 0 is the main screen; a missing screen falls back to 0 |
| `floor_board_size` | str | `"full"` | `"full"` or `"<width>x<height>"` |

1. `HxSettings.load(local)`: `d = local.read_user_json("maplejet.json")` (`{}` when missing or
   damaged). Each field is taken only when it has the right type and value: a bool; an int `>= 0`;
   `"full"` or a fullmatch of `SIZE_PATTERN` with width at least 640 and height at least 360. Anything
   else gets the default. Called on the Tk thread in `make_services` (storage.md §7.1 rule 5).
2. `save(local)`: `local.write_user_json("maplejet.json", {"floor_board_at_startup": ...,
   "floor_board_screen": ..., "floor_board_size": ...})`, run with `bg.run`.

`UserSettingsDialog(root, services)`, title "My MapleJet settings", opened from the tab's "My
settings...":

```
┌ My MapleJet settings ─────────────────────────────────────────────────┐
│ Floor board                                                           │
│ [x] Open the floor board when Stripes starts                          │  at_startup_var
│ Screen  [2: 1920 x 1080            v]                                  │  screen_box
│ Size    (•) Full screen   ( ) Window  [1280] x [720]                   │  size_var, width_var, height_var
│ The floor board opens whenever Stripes starts on this PC, including   │
│ when you sign in to Windows.                                          │
│ [Open the floor board now]                  [Cancel]   [ Save ]       │  open_button, save_button
└───────────────────────────────────────────────────────────────────────┘
```

1. `screen_box` (`ChoiceBox` with keys `"0"`, `"1"`, ...): one entry per `hx_win.monitors()` item,
   "{n}: {width} x {height} (main)" for the primary one and "{n}: {width} x {height}" for the others
   (`n` counts from 1); off Windows one entry, "This screen".
2. Width Spinbox 640–7680, height 360–4320, enabled only for "Window".
3. The note under the size: "The floor board opens whenever Stripes starts on this PC, including when
   you sign in to Windows."
4. Save: `services.settings` is updated in place and saved with `bg.run`; when the floor board is open,
   it is closed and opened again with the new screen and size. Escape and Cancel close without saving.
5. "Open the floor board now": `nav.open_floor_board()` with the dialog's current values (saved first).

## 15. hx_ui: services, Navigator, start-up and shut-down

### 15.1 make_services

`make_services(root, *, config_path, bg, version, clock=SYSTEM_CLOCK, timings=TIMINGS, identity=None,
fs=None, gates=PROCESS_GATES, key=None)` builds everything and starts nothing:

1. `identity = identity or hx_win.current_identity()`.
2. `paths = hx_share.paths(config_path)`; `bus = EventBus()`.
3. `store = ShareStore(paths.share_root, identity=identity, clock=clock, fs=fs, timings=timings,
   version=version)`; `local = LocalStore(paths, clock=clock)`.
4. `data = DataService(store=store, local=local, bus=bus, identity=identity, clock=clock,
   timings=timings)`.
5. `clients = ClientPool(key=key if key is not None else hx_protocol.release_key(), clock=clock,
   gates=gates, timings=timings)`; `time = TimeSource([], clock=clock)`.
6. `poller = Poller(store=store, data=data, clients=clients, time=time, bus=bus, identity=identity,
   version=version, clock=clock, timings=timings, local=local)` (states.md §11.3).
7. `watcher = StatusWatcher(store=store, data=data, poller=poller, bus=bus, identity=identity,
   clock=clock, timings=timings)`.
8. `settings = HxSettings.load(local)`; `nav = Navigator(root)`.
9. `services = HxServices(version=version, clock=clock, timings=timings, bus=bus, bg=bg,
   identity=identity, paths=paths, store=store, local=local, data=data, clients=clients, time=time,
   poller=poller, watcher=watcher, settings=settings, nav=nav)`; `nav.services = services`.
10. `hx_ui_common.setup_styles(root)`.
11. `bus.subscribe(DataUpdate, ...)`: `time.set_hosts([h for h in (company.time_server,
    company.time_server_fallback) if h])`.

`HxServices.pipeline()`, `actions()` and `test_run(plan)` build a new `SendPipeline`, `PrinterActions`
or `TestPrintRun` from the services' store, data, clients, time, identity, clock and timings each call.

### 15.2 start_services

`start_services(root, *, config_path, bg, version)` returns at once and never touches the share on the
Tk thread:

1. `services = make_services(root, config_path=config_path, bg=bg, version=version)`.
2. `gc.disable()` and a `root.after(2000, ...)` loop calling `gc.collect()` (architecture §7.1 rule 3).
3. A `root.after(100, ...)` loop calling `services.bus.pump()`; a `tk.TclError` (the root is gone)
   ends the loop.
4. `nav.sounder = AlertSounder(services)`; `nav` subscribes to `BoardUpdate` to call
   `update_monitor_flag()`.
5. `data.start()`, `poller.start()`, `watcher.start()`.

### 15.3 Navigator

`Navigator(root)` holds `root`, `services`, `windows: dict[str, HxWindow]`, `tab` (the `MapleJetTab`,
set by its constructor), `tab_shown`, `sounder`, `offline_sends` and the last monitor flag. `hx_ui`
imports `hx_ui_send` and `hx_ui_admin` inside the Navigator methods that open their windows, not at
the top of the module, so `hx_ui`, `hx_ui_common` and `hx_ui_monitor` build and test before those two
exist (they are built in parallel).

| Method | Role needed | Does |
|---|---|---|
| `open_send(prefill, line_ids)` | sender | §5.1 |
| `open_result(send_id)` | anyone (only the sender gets buttons) | `ResultWindow.from_log(root, services, send_id)` |
| `open_failure(result, plan)` | (internal) | `FailureWindow(root, services, result, plan)` unless one is open for that send |
| `open_monitor()` | anyone | one `MonitorWindow`; then `update_monitor_flag()` |
| `open_floor_board()` | anyone | §10.1 |
| `open_admin(page, record_id)` | admin | §11.1 |
| `open_test_print(film_id, machine_id, kind)` | admin | §12 |
| `stop(machine_ids, reason, send_id)` | sender | `StopDialog(root, services, machine_ids=..., reason=..., send_id=...)` |
| `resume(machine_ids)` | sender | `ResumeDialog(...)` |
| `set_clocks(line_ids)` | sender | `SetClocksDialog(...)` |
| `window(key)` | | the open window with that key, else None |
| `floor_board_open()` | | `window("floor") is not None` |
| `update_monitor_flag()` | | below |

1. Every `HxWindow` with a `key` registers with `nav.windows` in its constructor and is removed on its
   `<Destroy>` (when `event.widget is window`). Opening a key that is open shows it (§2.5).
2. `update_monitor_flag()`: `monitor_open = (tab_shown and root.winfo_viewable()) or window("monitor")
   is not None or floor_board_open()`; `floor = floor_board_open()`. When `(monitor_open, floor)`
   differs from the last value, it calls `poller.set_monitor_open(monitor_open, floor_board=floor)`
   (states §13: while the tab is shown, the monitor is open or the floor board is open), and, when
   `floor` changed, `sounder.evaluate(watcher.latest)`. It is called on tab changes, when the monitor or
   floor board opens or closes, and on every `BoardUpdate` (which also catches the main window being
   hidden to the tray).
3. **Sends made without the share** (send.md §14.1). `note_offline_send(send_id)` (called by the Send
   window when a result has a `no_lock` machine, §5.8) adds the id to `offline_sends`;
   `note_offline_answer(send_id, ids, everything_right)` (called by the result window when `confirm`
   raised a `ShareError` for a Send in `offline_sends`) remembers the answer. The Navigator subscribes
   to `ShareHealth`; on each `ShareHealth(ok=True)` while `offline_sends` isn't empty and no such run is
   going, it runs one `bg.run` that, for each id: `entry = data.find_send(id)`; when found,
   `services.pipeline().record_offline(entry)`, then, when an answer was remembered,
   `pipeline.confirm(id, ids, everything_right)`; an id is dropped from the list once both succeeded
   (errors keep it for the next `ShareHealth`). The list lives in memory only: a restart loses it, and
   the machine then shows `drift` until the next Send (architecture §12.1).

### 15.4 can_quit and stop_services

1. `can_quit(parent, services)`: `r = running()`; empty → True. Otherwise `ask(parent, Msg(N_("{running}
   If you quit now, a printer may be left halfway through. Quit anyway?"), {"running": r[0]}),
   yes=N_("Quit anyway"), no=N_("Keep Stripes open"))`. The question is asked once per quit
   (architecture §10.6 rule 2).
2. `stop_services(services, timeout_s=2.0)` is safe on services that were never started: when
   `nav.sounder` is None or the services were never started, it only calls `hx_win.stop_alert()`, and
   each `stop()` returns at once when its thread never ran. Otherwise: `nav.sounder.stop()` first
   (silence at once); then
   `poller.stop(timeout_s)` (it hands its printers over), `watcher.stop(timeout_s)` and
   `data.stop(timeout_s)` on three helper threads started together and joined against one deadline of
   `timeout_s + 0.5` s; then `hx_win.stop_alert()`. Running Send or test threads are daemon threads:
   their locks go stale and the poller handles it (architecture §7.5 rule 5). Windows are destroyed by
   `App.destroy()`.

### 15.5 startup, open_monitor, language_changed

1. `startup(root, services)`: when `services.settings.floor_board_at_startup`, `root.after(1000,
   services.nav.open_floor_board)`. This happens whether or not the main window started hidden
   (`--tray`): the floor-board PC shows the board at sign-in.
2. `open_monitor(root, services)`: `services.nav.open_monitor()` (the tray's "MapleJet monitor").
3. `language_changed(services)`: `nav.tab.retranslate()`, then `w.retranslate()` for each
   `open_windows()`. Bag text isn't touched: it is in `BagText`/`PictureView` canvases and entries,
   which `retranslate()` never sets.

### 15.6 self_check

`self_check(root)` is implemented exactly as deploy.md §1.4 and §5 specify (its checks 1 to 10, with
private helpers in `hx_ui`); ui.md adds no check of its own. What that needs from the UI modules:
`hx_ui_common.bmp_to_image` (check 9), `assets/alert.wav` readable by `wave` within deploy.md §2.4's
limits (check 8; §9.4 here: 3.0 s, mono, 16-bit, 22,050 Hz, about 132 KB), and a `MapleJetTab` that
builds from `make_services` with no share, a `FakeClock` and an identity without SIDs (check 10): it
then shows "Loading the MapleJet settings..." or the not-set-up problem (§4.6) and starts nothing.

### 15.7 MapleJetTab

`MapleJetTab(master, services)` builds §4, sets `services.nav.tab = self`, subscribes to `BoardUpdate`,
`DataUpdate`, `ShareHealth` and `PollerUpdate` (unsubscribed in `destroy()`), and re-applies the role
gating (§2.7) on every `DataUpdate`. Test attributes: §4.1.

## 16. Texts and Spanish

Rules (with architecture §10.6 and `test_i18n.py`):

1. Each UI module's `ES` holds the texts below for that module; `i18n.add_spanish(ES)` runs at import.
   A text used by two or more UI modules is in `hx_ui_common.ES` only, and the others use it without
   registering it again.
2. Texts that other files already register are used with their existing Spanish and are not listed
   again: from `i18n.ES` ("Close", "Cancel", "Yes", "No", "Refresh now", "Loading...", "Checking...",
   "Printing", "Name", "Status", "Size", "Port", "IP address", "Item", "Text", "Right", "Move up",
   "Move down", "Pictures", "All files", "Saved {name}.", "Give the printer a name.", "Another printer
   already has that name.", "updated {time}"); from `hx_state` (every §10 text of states.md, including
   "Message {name} in slot {slot}.", "Not checked yet", "checked {seconds} s ago", "NOT MONITORED");
   from `hx_share` (storage.md §13.7, including "Only Stripes Administrators can change this."); and
   send.md's `STEP_TEXTS`, `NOTE_TEXTS` and messages.
3. No two English texts may share a Spanish text, and no Spanish text may equal a different English
   text (`test_i18n.py`); `add_spanish` raises on a conflict, so a clash fails at import.
4. Backslashes in literals are doubled: the text shown as "DOMAIN\name" is written `"DOMAIN\\name"`.
5. Bag text, record names and typed values never appear in these tables.

### 16.1 hx_ui_common

| English | Spanish |
|---|---|
| Stripes hit a problem: {error} | Stripes tuvo un problema: {error} |
| {first} and {last} | {first} y {last} |
| (origin unknown) | (origen desconocido) |
| Save | Guardar |
| Revert | Deshacer cambios |
| Go back | Volver |
| Retry | Reintentar |
| Acknowledge | Reconocer |
| Stop printing | Detener la impresión |
| Stop printing... | Detener la impresión... |
| Resume printing | Reanudar la impresión |
| Send... | Enviar... |
| Set clocks... | Ajustar relojes... |
| Fix and send again | Corregir y enviar de nuevo |
| Try again | Intentar de nuevo |
| Leave it | Dejarlo así |
| Only Stripes MapleJet Senders can do this. | Solo los miembros de Stripes MapleJet Senders pueden hacer esto. |
| Only Stripes Administrators can do this. | Solo los miembros de Stripes Administrators pueden hacer esto. |
| Front photo | Foto del frente |
| Back photo | Foto de atrás |
| Artwork | Arte |
| Spec sheet | Hoja de especificaciones |
| Other file | Otro archivo |
| No photo | Sin foto |
| Code on the back of the bag | Código en la parte de atrás de la bolsa |
| Code on the front of the bag | Código en el frente de la bolsa |
| Encoder | Encoder |
| Delay | Retardo |
| Paper (Boxtron) | Papel (Boxtron) |
| Plastic | Plástico |
| White | Blanca |
| Black | Negra |
| In development | En desarrollo |
| Active | Activo |
| Not active | Inactivo |
| Line | Línea |
| Machine | Máquina |
| Machines | Máquinas |
| Product | Producto |
| Film | Película |
| Print text | Texto de impresión |
| Print texts | Textos de impresión |
| Lot | Lote |
| Floor board | Tablero de planta |
| No lines are set up yet. | Todavía no hay líneas configuradas. |
| Stop it at the printer now | Deténgala en la impresora ahora |
| Stripes couldn't stop printing on {machines}. | Stripes no pudo detener la impresión en {machines}. |
| Press Print On/Off on that printer now. Its card shows "Stop it at the printer" until Stripes reads that printing is off. | Presione Print On/Off en esa impresora ahora. Su tarjeta muestra "Deténgala en la impresora" hasta que Stripes lea que la impresión está apagada. |
| You can refresh once every {seconds} s. | Puede actualizar una vez cada {seconds} s. |
| Full size | Tamaño real |
| Fit to window | Ajustar a la ventana |
| Open the file | Abrir el archivo |
| Loading the original photo... | Cargando la foto original... |
| Showing the preview: Stripes couldn't read the original photo. | Se muestra la vista previa: Stripes no pudo leer la foto original. |
| Drag the photo to move around it. | Arrastre la foto para moverse por ella. |
| Stripes can't reach the server, so the original can't be opened now. | Stripes no puede conectar con el servidor, así que el original no se puede abrir ahora. |
| Opening files works on Windows only. The file is at {path}. | Abrir archivos solo funciona en Windows. El archivo está en {path}. |
| Stripes couldn't open the file: {error} | Stripes no pudo abrir el archivo: {error} |

### 16.2 hx_ui_send

| English | Spanish |
|---|---|
| MapleJet Send | Envío MapleJet |
| Send to the bag printers | Enviar a las impresoras de bolsas |
| Sending as {user} on {computer} | Enviando como {user} en {computer} |
| Using this PC's copy of the templates, as of {time}. The server can't be reached, so every machine's result will be amber. | Se usa la copia de las plantillas de esta PC, del {time}. No se puede conectar con el servidor, así que el resultado de cada máquina será ámbar. |
| {n} log entries waiting to upload. | {n} registros esperando para subirse. |
| 1 log entry waiting to upload. | 1 registro esperando para subirse. |
| Lines: | Líneas: |
| Close the Send window? Your choices will be lost. | ¿Cerrar la ventana de envío? Se perderán sus opciones. |
| Keep it open | Dejarla abierta |
| Replace the choices in the Send window? | ¿Reemplazar las opciones de la ventana de envío? |
| Replace them | Reemplazarlas |
| Keep them | Conservarlas |
| A Send is running. Wait for it to finish. | Hay un envío en curso. Espere a que termine. |
| No films are ready for this line's cartridge type. | No hay películas listas para el tipo de cartucho de esta línea. |
| ({min} to {max} digits) | (de {min} a {max} dígitos) |
| (at most {n} characters) | (máximo {n} caracteres) |
| (optional) | (opcional) |
| Countries, in the order they print: | Países, en el orden en que se imprimen: |
| Add | Agregar |
| Remove | Quitar |
| Origin is pre-printed on this film: | El origen viene preimpreso en esta película: |
| Origin on the bag: | Origen en la bolsa: |
| Same origin as now. | Mismo origen que ahora. |
| {machine} isn't ticked and is still printing: | {machine} no está marcada y sigue imprimiendo: |
| This line will print two origins. | Esta línea imprimirá dos orígenes. |
| I know this line will print two origins | Sé que esta línea imprimirá dos orígenes |
| (origin unknown) → | (origen desconocido) → |
| Prints: | Imprime: |
| Delay {ms} ms | Retardo de {ms} ms |
| Print position {mm} mm | Posición de impresión de {mm} mm |
| Still needed: | Falta: |
| Send to {n} machines | Enviar a {n} máquinas |
| Send to 1 machine | Enviar a 1 máquina |
| {line} isn't active any more. | {line} ya no está activa. |
| A Send to {machines} is still running. | Todavía hay un envío en curso a {machines}. |
| Sending to {lines} | Enviando a {lines} |
| Getting ready... | Preparando... |
| Sending in | Se envía en |
| Cancelling... | Cancelando... |
| Hang on, we're working on it | Espere, estamos trabajando en ello |
| Press Cancel to stop the Send. | Presione Cancelar para detener el envío. |
| The Send is running. Wait for it to finish. | El envío está en curso. Espere a que termine. |
| Stripes hit a problem during the Send. Check each machine's card on the MapleJet tab. | Stripes tuvo un problema durante el envío. Revise la tarjeta de cada máquina en la pestaña MapleJet. |
| Check the new messages | Revise los mensajes nuevos |
| Sent by {user} on {computer} at {time}. | Enviado por {user} en {computer} el {time}. |
| Look at each printer's picture. Wait for the first bags, then confirm each line. | Mire la imagen de cada impresora. Espere las primeras bolsas y luego confirme cada línea. |
| Sent from this PC's copy of the templates (as of {time}). Check the first bags on each machine. | Enviado con la copia de las plantillas de esta PC (del {time}). Revise las primeras bolsas de cada máquina. |
| The time server didn't answer, so the PC's time was used. | El servidor de hora no respondió, así que se usó la hora de la PC. |
| The send log is waiting on this PC. It uploads when the server is back. | El registro del envío está esperando en esta PC. Se subirá cuando el servidor vuelva. |
| Live and checked twice. Waiting for you to confirm. | Activo y revisado dos veces. Esperando su confirmación. |
| Before: | Antes: |
| Same origin as before. | Mismo origen que antes. |
| {machine}: the Send failed. See the red window. | {machine}: el envío falló. Vea la ventana roja. |
| Only {user} can confirm this Send. | Solo {user} puede confirmar este envío. |
| Everything is right | Todo está bien |
| Something's wrong | Algo está mal |
| Confirmed by you at {time}. | Confirmado por usted el {time}. |
| You said something's wrong. These machines stay red until a new Send. | Usted indicó que algo está mal. Estas máquinas quedan en rojo hasta un nuevo envío. |
| Printing stopped. This line needs a new Send. | Impresión detenida. Esta línea necesita un nuevo envío. |
| Wait until the confirmation is saved. | Espere a que se guarde la confirmación. |
| Lines you haven't confirmed stay amber "Send not confirmed". You can confirm them later from the machine's card on the MapleJet tab. | Las líneas que no confirmó quedan en ámbar "Envío no confirmado". Puede confirmarlas después desde la tarjeta de la máquina en la pestaña MapleJet. |
| Loading the Send... | Cargando el envío... |
| This PC can't find the full record of this Send. These are the printers' latest pictures. | Esta PC no encuentra el registro completo de este envío. Estas son las imágenes más recientes de las impresoras. |
| Nothing in this Send is waiting for confirmation. | Nada de este envío espera confirmación. |
| Send failed | El envío falló |
| Sent by {user} at {time}. Stripes stopped at the step that failed and did nothing else. | Enviado por {user} el {time}. Stripes se detuvo en el paso que falló y no hizo nada más. |
| Failed at: {step} | Falló en: {step} |
| Where | Dónde |
| What | Qué |
| Sent | Enviado |
| Read back | Leído |
| Last confirmed printing: {name} | Lo último confirmado que imprimía: {name} |
| Printing before this Send (not confirmed): {name} | Lo que imprimía antes de este envío (sin confirmar): {name} |
| Try again was cancelled. | Se canceló Intentar de nuevo. |
| Stripes doesn't know what this machine was last confirmed printing. | Stripes no sabe qué fue lo último confirmado que imprimía esta máquina. |
| {machines} went live. Confirm them in the result window. | {machines} quedaron activas. Confírmelas en la ventana de resultados. |
| {machine} went live. Confirm it in the result window. | {machine} quedó activa. Confírmela en la ventana de resultados. |
| Only {user} can choose Try again or Leave it. | Solo {user} puede elegir Intentar de nuevo o Dejarlo así. |
| Choose Try again or Leave it. | Elija Intentar de nuevo o Dejarlo así. |
| Leave {machines} red? The line stays red until a Send succeeds, the line's notify user is alerted, and this is logged. | ¿Dejar {machines} en rojo? La línea queda en rojo hasta que un envío funcione, se avisa al usuario a notificar de la línea y queda registrado. |
| Stop printing on {machines}? | ¿Detener la impresión en {machines}? |
| You said something's wrong with the new message. Printing is turned off and read back twice. Resume stays blocked for these machines, so the rejected message can't be restarted by accident. | Usted indicó que algo está mal con el mensaje nuevo. Se apaga la impresión y se revisa dos veces. Reanudar queda bloqueado en estas máquinas, para que el mensaje rechazado no se reinicie por accidente. |
| Printing is turned off and read back twice to make sure it's off. | Se apaga la impresión y se revisa dos veces para asegurar que quedó apagada. |
| Stopping printing... | Deteniendo la impresión... |
| Printing stopped on {machines}. {line} needs a new Send. | Se detuvo la impresión en {machines}. {line} necesita un nuevo envío. |
| Printing stopped on {machines}. | Se detuvo la impresión en {machines}. |
| {machine}: {problem} | {machine}: {problem} |
| Wait until Stripes has finished stopping printing. | Espere a que Stripes termine de detener la impresión. |
| Stripes is stopping printing on {machines}. | Stripes está deteniendo la impresión en {machines}. |
| Stripes is resuming printing on {machines}. | Stripes está reanudando la impresión en {machines}. |
| Stripes is setting the clocks on {machines}. | Stripes está ajustando los relojes en {machines}. |
| Loading what's on each printer... | Cargando lo que tiene cada impresora... |
| This is the message of the last confirmed Send. | Este es el mensaje del último envío confirmado. |
| Only the machines that passed the check are resumed. | Solo se reanudan las máquinas que pasaron la revisión. |
| Printing resumed on {machines}. | Se reanudó la impresión en {machines}. |
| Set clocks | Ajustar relojes |
| Set the clock of every printer on {lines} from the time server? | ¿Ajustar el reloj de todas las impresoras de {lines} con el servidor de hora? |
| Stripes sets the date, the time and the midnight date change. Nothing else on the printers changes. | Stripes ajusta la fecha, la hora y el cambio de fecha a medianoche. No cambia nada más en las impresoras. |
| Clock set | Reloj ajustado |

### 16.3 hx_ui_monitor

| English | Spanish |
|---|---|
| Stop line... | Detener la línea... |
| Resume line... | Reanudar la línea... |
| Stop... | Detener... |
| Resume... | Reanudar... |
| Confirm... | Confirmar... |
| Set up... | Configurar... |
| acknowledged by {user} | reconocida por {user} |
| not acknowledged | sin reconocer |
| {n} red | {n} en rojo |
| {n} amber | {n} en ámbar |
| {n} in progress | {n} en curso |
| {n} grey | {n} en gris |
| {n} green | {n} en verde |
| MapleJet alert | Alerta MapleJet |
| since {time} | desde {time} |
| The sound repeats until someone acknowledges. | El sonido se repite hasta que alguien lo reconozca. |
| Ask a Stripes MapleJet Sender or a Stripes Administrator to acknowledge it. | Pida a un miembro de Stripes MapleJet Senders o de Stripes Administrators que la reconozca. |
| Open MapleJet monitor | Abrir el monitor MapleJet |
| MapleJet floor board | Tablero de planta MapleJet |
| My MapleJet settings | Mi configuración de MapleJet |
| Open the floor board when Stripes starts | Abrir el tablero de planta cuando Stripes inicie |
| Screen | Pantalla |
| Full screen | Pantalla completa |
| Window | Ventana |
| The floor board opens whenever Stripes starts on this PC, including when you sign in to Windows. | El tablero de planta se abre cada vez que Stripes inicia en esta PC, también al iniciar sesión en Windows. |
| {n}: {width} x {height} (main) | {n}: {width} x {height} (principal) |
| {n}: {width} x {height} | {n}: {width} x {height} |
| This screen | Esta pantalla |
| Open the floor board now | Abrir el tablero de planta ahora |

### 16.4 hx_ui_admin

| English | Spanish |
|---|---|
| MapleJet admin | Administración de MapleJet |
| The server can't be reached. Changes can't be saved now. | No se puede conectar con el servidor. Ahora no se pueden guardar cambios. |
| Company | Empresa |
| Lines | Líneas |
| Printers | Impresoras |
| Products and films | Productos y películas |
| Machine + film | Máquina + película |
| Show not active | Mostrar inactivos |
| New line | Línea nueva |
| New machine | Máquina nueva |
| New printer | Impresora nueva |
| New product | Producto nuevo |
| New print text | Texto de impresión nuevo |
| New adjustment... | Ajuste nuevo... |
| Not saved yet. | Todavía no se guarda. |
| Revision {n} · saved by {user} at {time} | Revisión {n} · guardado por {user} el {time} |
| Discard your changes to {name}? | ¿Descartar sus cambios en {name}? |
| Discard | Descartar |
| Keep editing | Seguir editando |
| {field}: type a number. | {field}: escriba un número. |
| {field}: type a whole number. | {field}: escriba un número entero. |
| {field} is needed. | {field}: falta este dato. |
| Saving... | Guardando... |
| Reload | Volver a cargar |
| Stripes couldn't save this: {error} | Stripes no pudo guardar esto: {error} |
| {user} changed this record at {time}. Reload to see their change; saving now would be refused. | {user} cambió este registro el {time}. Vuelva a cargarlo para ver su cambio; si guarda ahora, se rechazará. |
| Code | Código |
| Cartridge | Cartucho |
| Notify user | Usuario a notificar |
| Placement | Colocación |
| Printer | Impresora |
| UID | UID |
| Password | Contraseña |
| Customer | Cliente |
| Settings revision | Revisión de ajustes |
| Revision | Revisión |
| Adjustment | Valor del ajuste |
| Set by | Ajustado por |
| Set at | Ajustado el |
| Time server | Servidor de hora |
| Fallback | Alternativo |
| Countries (spelled exactly as printed) | Países (escritos tal como se imprimen) |
| Country | País |
| Also refused in other text | También rechazado en otros textos |
| Offered | Se ofrece |
| Add country | Agregar país |
| Apply | Aplicar |
| Inks (the names the printers report) | Tintas (los nombres que reportan las impresoras) |
| Ink name | Nombre de la tinta |
| Colour | Color |
| Add ink | Agregar tinta |
| Remove ink | Quitar tinta |
| Lot: from | Lote: de |
| to | a |
| digits | dígitos |
| Free text: at most | Texto libre: máximo |
| characters | caracteres |
| Administrators group | Grupo de administradores |
| Senders group | Grupo de remitentes |
| SID | SID |
| Look up | Buscar |
| You are in this group. | Usted está en este grupo. |
| You aren't in this group. | Usted no está en este grupo. |
| Standard fonts | Fuentes estándar |
| Change these only when every printer's font list has changed: Stripes refuses to send to a printer whose fonts differ. | Cámbielas solo cuando haya cambiado la lista de fuentes de todas las impresoras: Stripes no envía a una impresora con fuentes distintas. |
| Which counter counts bags | Qué contador cuenta las bolsas |
| Print counter (PMCNT) | Contador de impresión (PMCNT) |
| Message counter (MSCNT) | Contador de mensajes (MSCNT) |
| No counter: time each test step | Sin contador: medir cada paso de prueba por tiempo |
| Windows couldn't find a group called {name}. | Windows no encontró un grupo llamado {name}. |
| You aren't in {group}. After saving, you won't be able to change the MapleJet settings. Save anyway? | Usted no está en {group}. Después de guardar no podrá cambiar la configuración de MapleJet. ¿Guardar de todos modos? |
| Save anyway | Guardar de todos modos |
| With this country list, these records contain words about origin and can't be sent until they're fixed: {list} Save anyway? | Con esta lista de países, estos registros contienen palabras sobre el origen y no se pueden enviar hasta corregirlos: {list} ¿Guardar de todos modos? |
| Printed code | Código impreso |
| Up to 3 capital letters or digits, printed by a Line code item. | Hasta 3 letras mayúsculas o dígitos, impresos por un elemento Código de línea. |
| Up to 3 capital letters or digits, printed by a Machine code item. | Hasta 3 letras mayúsculas o dígitos, impresos por un elemento Código de máquina. |
| Cartridge type | Tipo de cartucho |
| Only films of the same type are offered at Send. | Al enviar solo se ofrecen películas del mismo tipo. |
| Me | Yo |
| DOMAIN\name. Gets this line's alerts on whichever PC they're signed in to. | DOMINIO\nombre. Recibe las alertas de esta línea en cualquier PC donde inicie sesión. |
| Add a machine | Agregar una máquina |
| Type the notify user as DOMAIN\name, for example EXAMPLE\jsmith. | Escriba el usuario a notificar como DOMINIO\nombre, por ejemplo EXAMPLE\jsmith. |
| A printer belongs to one machine. | Una impresora pertenece a una sola máquina. |
| Placement mode | Modo de colocación |
| Delay: each film's delay after the trigger, plus this machine's delay adjustment for that film. No calibration. | Retardo: el retardo de cada película después del disparo, más el ajuste de retardo de esta máquina para esa película. Sin calibración. |
| Encoder: each film's print position in mm, worked out from this machine's head distance. Calibrate it once, at installation. | Encoder: la posición de impresión de cada película en mm, calculada con la distancia del cabezal de esta máquina. Calíbrela una vez, al instalarla. |
| Print speed (cm/min) | Velocidad de impresión (cm/min) |
| Needed before this machine can be sent to. | Se necesita antes de poder enviar a esta máquina. |
| Rotate | Girar |
| Mirror | Espejo |
| Depend on how the printer is mounted. | Dependen de cómo está montada la impresora. |
| Seconds per bag | Segundos por bolsa |
| Used to time test steps when no counter counts bags. | Se usa para medir los pasos de prueba cuando ningún contador cuenta bolsas. |
| Encoder (encoder machines only) | Encoder (solo máquinas con encoder) |
| Head distance: {mm} mm (calibrated by {user} at {time}) | Distancia del cabezal: {mm} mm (calibrada por {user} el {time}) |
| Head distance: not calibrated. This machine can't be sent to until it is. | Distancia del cabezal: sin calibrar. No se puede enviar a esta máquina hasta calibrarla. |
| Calibrate... | Calibrar... |
| Expected wheel size (mm) | Tamaño de rueda esperado (mm) |
| Expected pulses per turn | Pulsos por vuelta esperados |
| Stripes reads these from the printer every day and raises an alert if they change. | Stripes los lee de la impresora cada día y da una alerta si cambian. |
| Now: | Ahora: |
| Change the placement mode of {machine}? Other film values and adjustments then apply, so check every film on this machine with a test print. | ¿Cambiar el modo de colocación de {machine}? Entonces se usan otros valores de película y otros ajustes, así que revise cada película en esta máquina con una impresión de prueba. |
| Change it | Cambiarlo |
| The printer's fixed address. | La dirección fija de la impresora. |
| 80 for a real printer. | 80 para una impresora real. |
| Checked on every poll and Send. | Se revisa en cada consulta y cada envío. |
| No password saved yet. Stripes can't check this printer until one is. | Todavía no hay contraseña guardada. Stripes no puede revisar esta impresora hasta que la haya. |
| A password is saved. Type a new one only to change it. | Hay una contraseña guardada. Escriba una nueva solo para cambiarla. |
| The saved password was made by a different Stripes build. Type it again. | La contraseña guardada se hizo con otra versión de Stripes. Escríbala de nuevo. |
| Stripes keeps only a scrambled token made from the password, never the password itself. | Stripes guarda solo un código cifrado hecho con la contraseña, nunca la contraseña. |
| Type the printer's IP address, for example 192.0.2.10. | Escriba la dirección IP de la impresora, por ejemplo 192.0.2.10. |
| {name} already has this address. | {name} ya tiene esta dirección. |
| The port must be a number from 1 to 65535. | El puerto debe ser un número de 1 a 65535. |
| The UID must be a whole number from 1 to 4294967295. | El UID debe ser un número entero de 1 a 4294967295. |
| Used by: | La usa: |
| Not used by any machine yet. | Todavía no la usa ninguna máquina. |
| Add new version | Agregar versión nueva |
| A new artwork revision is a new film. It starts blank and needs its own test print. | Una revisión nueva del arte es una película nueva. Empieza en blanco y necesita su propia impresión de prueba. |
| Give the product a name. | Póngale un nombre al producto. |
| Another product already has that name. | Otro producto ya tiene ese nombre. |
| The customer name is too long. | El nombre del cliente es demasiado largo. |
| Films | Películas |
| Product: {name} | Producto: {name} |
| Up to 6 capital letters or digits, used in the printer's message name. A new film needs a new code. | Hasta 6 letras mayúsculas o dígitos, usados en el nombre del mensaje de la impresora. Una película nueva necesita un código nuevo. |
| Artwork revision | Revisión del arte |
| Only Active films are offered at Send. | Al enviar solo se ofrecen películas activas. |
| Settings revision {n} | Revisión de ajustes {n} |
| Goes up on every save. | Aumenta cada vez que se guarda. |
| Bag | Bolsa |
| Back film: the code goes on the back | Película de atrás: el código va atrás |
| Front film: the code goes on the front | Película del frente: el código va al frente |
| Paper or plastic | Papel o plástico |
| Must match the line's cartridge type. | Debe coincidir con el tipo de cartucho de la línea. |
| Ink colour | Color de tinta |
| Checked against the ink the printer reports. | Se compara con la tinta que reporta la impresora. |
| Film length (mm) | Largo de la película (mm) |
| The bag's cut-off length, from the spec sheet. Needed for encoder machines. | El largo de corte de la bolsa, de la hoja de especificaciones. Se necesita para máquinas con encoder. |
| Print position (mm) | Posición de impresión (mm) |
| From the bag's bottom edge, the edge that feeds in first. Needed for encoder machines. Found with the test print. | Desde el borde inferior de la bolsa, el borde que entra primero. Se necesita para máquinas con encoder. Se encuentra con la impresión de prueba. |
| Delay (ms) | Retardo (ms) |
| Time from the trigger to the print. Needed for delay machines. Found with the test print. | Tiempo desde el disparo hasta la impresión. Se necesita para máquinas con retardo. Se encuentra con la impresión de prueba. |
| Print length (mm) | Largo de la impresión (mm) |
| How long the code is along the film, for the warning when a print would still be running as the film stops. Leave empty if unknown. | Cuánto mide el código a lo largo de la película, para avisar cuando una impresión seguiría en curso al detenerse la película. Déjelo vacío si no se sabe. |
| Print settings | Ajustes de impresión |
| Font | Fuente |
| One font and size for the whole print text. | Una fuente y un tamaño para todo el texto de impresión. |
| Resolution | Resolución |
| Ultra fine | Ultrafina |
| Fine | Fina |
| Normal | Normal |
| Draft | Borrador |
| Bold | Negrita |
| Width | Ancho |
| Origin | Origen |
| The origin is pre-printed on the film | El origen viene preimpreso en la película |
| Only print texts without an origin item can be used. | Solo se pueden usar textos de impresión sin elemento de origen. |
| Only print texts with an origin item can be used. | Solo se pueden usar textos de impresión con elemento de origen. |
| Pre-printed origin | Origen preimpreso |
| Shown at Send, on the floor board and in the log. | Se muestra al enviar, en el tablero de planta y en el registro. |
| Default | Predeterminado |
| Pre-filled at Send. | Se llena solo al enviar. |
| Files and photos | Archivos y fotos |
| Originals stay on the server. Each PC keeps small previews of the photos. | Los originales se quedan en el servidor. Cada PC guarda vistas previas pequeñas de las fotos. |
| Print texts that don't suit this setting were unticked. | Se desmarcaron los textos de impresión que no corresponden a este ajuste. |
| Delay machines can't send this film until it has a delay. | Las máquinas con retardo no pueden enviar esta película hasta que tenga un retardo. |
| Encoder machines can't send this film until it has a film length and a print position. | Las máquinas con encoder no pueden enviar esta película hasta que tenga largo y posición de impresión. |
| Kind | Clase |
| Added by | Agregado por |
| Added | Agregado el |
| Add as | Agregar como |
| Add file... | Agregar archivo... |
| Open | Abrir |
| Remove from film | Quitar de la película |
| Choose a file to add | Elija un archivo para agregar |
| PDF files | Archivos PDF |
| The file is added. Save the film to keep it. | Se agregó el archivo. Guarde la película para conservarlo. |
| Stripes can't reach the server, so the file can't be opened now. | Stripes no puede conectarse con el servidor, así que ahora no se puede abrir el archivo. |
| Machine adjustments | Ajustes por máquina |
| Edit adjustment... | Editar ajuste... |
| Ignored during a test print. | No se usa durante una impresión de prueba. |
| Test print... | Impresión de prueba... |
| Save the film first. | Primero guarde la película. |
| Text before | Texto antes |
| Data | Datos |
| Text after | Texto después |
| Gap | Espacio |
| Below | Debajo |
| Fixed text | Texto fijo |
| Packed-on date | Fecha de empaque |
| Best-by date | Fecha de consumo preferente |
| Country of origin | País de origen |
| Free text | Texto libre |
| Machine code | Código de máquina |
| Line code | Código de línea |
| [lot] | [lote] |
| [countries] | [países] |
| [machine code] | [código de máquina] |
| [line code] | [código de línea] |
| days | días |
| months | meses |
| years | años |
| Add item | Agregar elemento |
| Remove item | Quitar elemento |
| A print text has at most one origin item. | Un texto de impresión tiene como máximo un elemento de origen. |
| Date format | Formato de fecha |
| prints | imprime |
| Offset | Desplazamiento |
| Always prints "Product of" and the countries chosen at Send. It has no text before or after. | Siempre imprime "Product of" y los países elegidos al enviar. No tiene texto antes ni después. |
| Question for the sender | Pregunta para quien envía |
| Most characters | Máximo de caracteres |
| Required | Obligatorio |
| Right of the previous item | A la derecha del elemento anterior |
| Below the previous item | Debajo del elemento anterior |
| Extra space before the item, in printer canvas units. | Espacio extra antes del elemento, en unidades del lienzo de la impresora. |
| Up to 3 capital letters or digits, used in the printer's message name. | Hasta 3 letras mayúsculas o dígitos, usados en el nombre del mensaje de la impresora. |
| Revision {n} | Revisión {n} |
| Goes up on every save. The send log records which one printed. | Aumenta cada vez que se guarda. El registro de envíos anota cuál se imprimió. |
| Preview with the longest values | Vista previa con los valores más largos |
| Longest line: {n} characters. | Línea más larga: {n} caracteres. |
| Approximate: the test print shows exactly how it prints. | Aproximada: la impresión de prueba muestra exactamente cómo se imprime. |
| Films that may use this print text | Películas que pueden usar este texto de impresión |
| Default for this film | Predeterminado de esta película |
| Only films whose origin isn't pre-printed are listed, because this print text has an origin item. | Solo se muestran películas sin origen preimpreso, porque este texto de impresión tiene un elemento de origen. |
| Only films with a pre-printed origin are listed, because this print text has no origin item. | Solo se muestran películas con origen preimpreso, porque este texto de impresión no tiene elemento de origen. |
| {film} uses this print text as its default. Choose another default in that film first. | {film} usa este texto de impresión como predeterminado. Primero elija otro predeterminado en esa película. |
| {film}: {problem} | {film}: {problem} |
| Save or revert your changes to {film} first. | Primero guarde o deshaga sus cambios en {film}. |
| The print text was saved, but these films weren't: | Se guardó el texto de impresión, pero estas películas no: |
| All machines | Todas las máquinas |
| All films | Todas las películas |
| Machine + film adjustment | Ajuste de máquina + película |
| Delay machine | Máquina con retardo |
| Encoder machine | Máquina con encoder |
| Position adjustment (mm) | Ajuste de posición (mm) |
| Delay adjustment (ms) | Ajuste de retardo (ms) |
| Can be negative. 0 means none. | Puede ser negativo. 0 significa ninguno. |
| Set by {user} at {time} | Ajustado por {user} el {time} |
| Choose a machine and a film. | Elija una máquina y una película. |
| The position adjustment must be between -1000 and 1000 mm. | El ajuste de posición debe estar entre -1000 y 1000 mm. |
| The delay adjustment must be between -10000 and 10000 ms. | El ajuste de retardo debe estar entre -10000 y 10000 ms. |
| Test print | Impresión de prueba |
| Test print: {film} ({product}) | Impresión de prueba: {film} ({product}) |
| {machine} is running empty film, with no product. | {machine} está corriendo película vacía, sin producto. |
| What to vary | Qué variar |
| from | de |
| in steps of | en pasos de |
| None ticked: the film's. | Sin marcar: los de la película. |
| Fonts side by side | Fuentes lado a lado |
| Add font | Agregar fuente |
| Remove font | Quitar fuente |
| {steps} steps · about {bags} bags. | {steps} pasos · unas {bags} bolsas. |
| Each step runs for {n} bags, counted by the printer. | Cada paso dura {n} bolsas, contadas por la impresora. |
| Each step runs for about {seconds} s, because no counter counts bags. | Cada paso dura unos {seconds} s, porque ningún contador cuenta bolsas. |
| Start test print | Iniciar impresión de prueba |
| The server can't be reached, so a test print can't take the printer's lock. | No se puede conectar con el servidor, así que la impresión de prueba no puede tomar el bloqueo de la impresora. |
| Start the test print on {machine}? It takes the printer for the whole test, about {bags} bags, and ends with printing off and slots 9 and 10 empty. | ¿Iniciar la impresión de prueba en {machine}? Ocupa la impresora durante toda la prueba, unas {bags} bolsas, y termina con la impresión apagada y las posiciones 9 y 10 vacías. |
| Start | Iniciar |
| A test print on {machine} is still running. | Todavía hay una impresión de prueba en curso en {machine}. |
| Step {step} of {of} | Paso {step} de {of} |
| Building and checking the next message... | Armando y revisando el siguiente mensaje... |
| Printing: {bags} of {per} bags | Imprimiendo: {bags} de {per} bolsas |
| Printing... | Imprimiendo... |
| Step done. | Paso terminado. |
| Stopping: printing off, then emptying slots 9 and 10. | Deteniendo: se apaga la impresión y luego se vacían las posiciones 9 y 10. |
| Printing is off and slots 9 and 10 are empty. | La impresión está apagada y las posiciones 9 y 10 están vacías. |
| Stop test | Detener la prueba |
| Finished: {done} of {of} steps. | Terminado: {done} de {of} pasos. |
| Stopped after {done} of {of} steps: {problem} | Se detuvo después de {done} de {of} pasos: {problem} |
| Printing is off and slots 9 and 10 are empty. {machine} needs a normal Send before production. | La impresión está apagada y las posiciones 9 y 10 están vacías. {machine} necesita un envío normal antes de producir. |
| Stripes couldn't empty slots 9 and 10. Check the printer, and send to it before production. | Stripes no pudo vaciar las posiciones 9 y 10. Revise la impresora y envíele un mensaje antes de producir. |
| Step | Paso |
| Mark | Marca |
| Position or delay | Posición o retardo |
| Fonts | Fuentes |
| Open the film | Abrir la película |
| Type the chosen values into the film. That's a normal edit; the film's status doesn't change. | Escriba en la película los valores elegidos. Es una edición normal; el estado de la película no cambia. |
| Stop the test print? | ¿Detener la impresión de prueba? |
| Keep it running | Dejarla seguir |
| Calibrate {machine} | Calibrar {machine} |
| Delay machines aren't calibrated. | Las máquinas con retardo no se calibran. |
| Calibrate once, at installation, with empty film. This procedure is still being confirmed on the test unit. | Calibre una vez, al instalar, con película vacía. Este procedimiento todavía se está confirmando en la unidad de prueba. |
| Film loaded for calibration | Película cargada para calibrar |
| 1. Rough head distance | 1. Distancia aproximada del cabezal |
| Save rough distance | Guardar la distancia aproximada |
| Saved: {mm} mm. | Guardado: {mm} mm. |
| Save the rough distance first. | Guarde primero la distancia aproximada. |
| 2. Distance scale | 2. Escala de distancia |
| Stripes prints two marks {mm} mm apart, at {a} mm and {b} mm after the trigger. | Stripes imprime dos marcas a {mm} mm de distancia, a {a} mm y {b} mm después del disparo. |
| Print the two marks | Imprimir las dos marcas |
| Mark A from the bag's bottom edge (mm) | Marca A desde el borde inferior de la bolsa (mm) |
| Mark B (mm) | Marca B (mm) |
| Gap: {gap} mm. | Espacio: {gap} mm. |
| The gap is within {mm} mm: the wheel size is right. | El espacio está dentro de {mm} mm: el tamaño de la rueda es correcto. |
| Correct the wheel size on the printer to {wheel} mm and set Expected wheel size on this machine's page to {wheel} mm, then print the marks again. | Corrija el tamaño de la rueda en la impresora a {wheel} mm y ponga {wheel} mm en Tamaño de rueda esperado en la página de esta máquina; luego vuelva a imprimir las marcas. |
| The gap is {measured} mm instead of {sent} mm. Correct the wheel size on the printer and set the same value as Expected wheel size on this machine's page, then print the marks again. | El espacio es de {measured} mm en vez de {sent} mm. Corrija el tamaño de la rueda en la impresora y ponga el mismo valor en Tamaño de rueda esperado en la página de esta máquina; luego vuelva a imprimir las marcas. |
| Print the marks first, then measure them. | Primero imprima las marcas y luego mídalas. |
| Print the mark first, then measure it. | Primero imprima la marca y luego mídala. |
| 3. Head distance | 3. Distancia del cabezal |
| Rough head distance by tape measure (mm) | Distancia aproximada del cabezal con cinta métrica (mm) |
| From the print head back along the film path to where a bag's bottom edge sits when the film stops. It can be longer than one bag. | Desde el cabezal, hacia atrás por el recorrido de la película, hasta donde queda el borde inferior de una bolsa cuando la película se detiene. Puede ser más larga que una bolsa. |
| Stripes prints one mark {h} mm after the trigger. | Stripes imprime una marca {h} mm después del disparo. |
| Print the mark | Imprimir la marca |
| Measured from the bag's bottom edge (mm) | Medido desde el borde inferior de la bolsa (mm) |
| Head distance: {mm} mm | Distancia del cabezal: {mm} mm |
| Last spacing check: {mm} mm by {user} at {time}. | Última revisión de espacio: {mm} mm por {user} el {time}. |
| Last head distance: {mm} mm by {user} at {time}. | Última distancia del cabezal: {mm} mm por {user} el {time}. |
| Save calibration | Guardar calibración |
| Saved. The head distance of {machine} is now {mm} mm. | Guardado. La distancia del cabezal de {machine} ahora es {mm} mm. |

### 16.5 hx_ui

| English | Spanish |
|---|---|
| Set all clocks... | Ajustar todos los relojes... |
| Admin... | Administrar... |
| My settings... | Mi configuración... |
| Signed in as {user}. | Sesión iniciada como {user}. |
| {signed_in} {can} | {signed_in} {can} |
| You can send, change settings, run test prints and acknowledge alerts. | Puede enviar, cambiar la configuración, hacer impresiones de prueba y reconocer alertas. |
| You can send and acknowledge alerts. | Puede enviar y reconocer alertas. |
| You can change settings, run test prints and acknowledge alerts. | Puede cambiar la configuración, hacer impresiones de prueba y reconocer alertas. |
| You can watch the lines. To send, ask IT to add you to {group}. | Puede ver las líneas. Para enviar, pida a TI que lo agregue a {group}. |
| Loading the MapleJet settings... | Cargando la configuración de MapleJet... |
| {machine}: {alert} | {machine}: {alert} |
| This PC is checking {n} printers. | Esta PC está revisando {n} impresoras. |
| This PC is checking 1 printer. | Esta PC está revisando 1 impresora. |
| This PC isn't checking any printers. | Esta PC no está revisando ninguna impresora. |
| This PC is checking {n} printers on its own while the server can't be reached. | Esta PC está revisando {n} impresoras por su cuenta mientras no se puede conectar con el servidor. |
| Open Admin... to add printers, lines and machines. | Abra Administrar... para agregar impresoras, líneas y máquinas. |
| {running} If you quit now, a printer may be left halfway through. Quit anyway? | {running} Si sale ahora, una impresora puede quedar a medias. ¿Salir de todos modos? |
| Quit anyway | Salir de todos modos |
| Keep Stripes open | Dejar Stripes abierto |

`i18n.ES` additions (stripes.py, §1.6; specified here, written by WP1 in `i18n.py`): "Zebra labels and
MapleJet bag printing" → "Etiquetas Zebra e impresión de bolsas MapleJet"; "MapleJet monitor" →
"Monitor MapleJet" (architecture §10.6).

## 17. Tests (test_hx_ui.py, test_hx_ui_send.py, test_hx_ui_admin.py)

Under xvfb on Linux and on `windows-latest`, with `unittest`; skipped as a whole when `tk.Tk()` raises
`TclError`. One withdrawn `tk.Tk()` per test class, destroyed in `tearDownClass` (architecture §9.3).

The tests are in three files, so the UI can be built in two parts at the same time (WP7 writing
`hx_ui_common`, `hx_ui_monitor` and `hx_ui`, WP8 `hx_ui_send` and `hx_ui_admin`; README.md "Work
packages"): `test_hx_ui.py` covers `hx_ui_common`, `hx_ui_monitor` and `hx_ui`; `test_hx_ui_send.py`
covers `hx_ui_send`; `test_hx_ui_admin.py` covers `hx_ui_admin`. The "File" column of §17.2 says which
holds each test. All three use the §17.1 set-up (each file has its own copy of the helper class, so no
file imports another test file).

### 17.1 Set-up

1. A temp folder; `world = FakeWorld(tmp, printers=[{}], timings=TIMINGS.scaled(0.05))` (one delay
   printer; tests that need encoder machines add `{"placement": "encoder"}`).
2. `unittest.mock.patch.dict(os.environ, {"STRIPES_V2": world.share_root, "STRIPES_V2_ROAMING": ...,
   "STRIPES_V2_LOCAL": ..., "STRIPES_V2_MACHINE": ...})` with three temp subfolders.
3. `services = hx_ui.make_services(root, config_path=None, bg=SyncBackground(), version="test",
   timings=TIMINGS.scaled(0.05), identity=world.identity("jsmith", admin=True, sender=True),
   fs=world.fs.view(), gates=GateRegistry(), key=hx_protocol.release_key())`, then
   `services.data.run_once()`.
4. `hx_ui_common.STRICT_THREADS = True`; `tell` and `ask` patched on `hx_ui_common` (every UI module
   calls them through the module, `hx_ui_common.tell(...)`, never through a `from ... import`, so one
   patch covers every screen).
5. `pump(until, timeout_s=10)`: loop `root.update()`, `services.bus.pump()`, a 10 ms real sleep, until
   `until()` is true; fail on timeout. No test sleeps more than 0.5 s in one go (architecture §9).
6. Boards for card and floor tests are built by hand (`BoardView`, `LineView`, `MachineView`,
   `FloorRow`), so they don't need the poller.

### 17.2 Tests

`test_stripes.py` is WP9's (README.md "Work packages", architecture §9.2): the two tests marked for
it need `stripes.App` with the architecture §10.3 and §10.4 hooks and every UI module, so they run
last.

| Test | File | Checks |
|---|---|---|
| `test_tab_builds_and_gates_roles` | `test_hx_ui.py` | `MapleJetTab` builds for a plain user, a sender and an administrator: `send_button`/`clocks_button` mapped only for the sender, `admin_button` only for the administrator; `who_label` texts of §4.2 |
| `test_zebra_shortcut_guard` | `test_stripes.py` | `stripes.App._zebra_shortcut(fake_self, event, action)` with `fake_self` holding a two-tab notebook: the action runs for a widget on the Zebra tab while it is selected; not while the MapleJet tab is selected; never for a widget inside an `HxWindow` or the `ask` dialog |
| `test_send_flow` | `test_hx_ui_send.py` | Send window: tick Line 1, choose the product, the film (no film pre-selected before), the default print text is pre-filled, lot "4417", countries USA then Canada; `origin_text.get() == "Product of USA and Canada"`; `change_texts` highlight against the board's origin; `send_button` enabled and reads "Send to 1 machine"; invoke; pump until a `ResultWindow` for the send is open; its `pictures[mid].bmp` is the second running read; invoke "Everything is right"; the machine's expected state is `confirmed`; the fake's `writes` ⊆ `ALLOWED_WRITE_TAGS`, `max_in_flight == 1`, no `violations` |
| `test_film_cards` | `test_hx_ui_send.py` | four films of the product with Pillow-drawn photos (never real artwork): cards in code-then-revision order, all in one row at 1000x780 and as wide as each other, with the print text, lot, countries and origin in view; tall photos 60x150 and a wide one 100x67 in the 100x150 box, "No photo" for the missing ones; clicking a card picks its film and marks it blue; at 760x560 fewer cards to a row; clicking a photo twice opens one `PhotoWindow`, whose `original` is the photo's `FileRef` and which reads the original |
| `test_delay_for_senders_and_the_working_for_administrators` | `test_hx_ui_send.py` | `position_labels[mid]` reads "Delay 480 ms" (`Hx.Small.TLabel`); `working_labels[mid]` the working for an administrator, and nothing with "µs" for a sender who isn't one; Spanish "Retardo de 480 ms" |
| `test_one_machine_went_live`, `test_machines_went_live` | `test_hx_ui_send.py` | the red window's `passed_label` for one machine ("Line 1 Bagger 2 went live. Confirm it in the result window.") and for two, in English and Spanish |
| `test_original_fitted_then_full_size`, `test_preview_when_the_original_cant_be_read`, `test_open_the_file`, `test_show_photo_opens_one_window_per_photo` | `test_hx_ui_common.py` | `PhotoWindow` with a Pillow-drawn 600x1600 original behind a threaded background: the preview first, the original read on a worker thread, the window taller than wide and at most 90% of the screen with its buttons in view, the picture fitted with its shape kept; Full size shows 600x1600 with scroll bars, grows the window, scrolls with the wheel and a drag, and Fit to window brings the size back; no path, a PDF or a missing file keep the preview with the note; Open the file calls `os.startfile` on a worker thread on Windows and only shows the path elsewhere; one window per photo |
| `test_send_blocked_banners` | `test_hx_ui_send.py` | data from the local copy shows `local_banner`; `can_send()` False shows `blocked_banner` and disables `send_button`; Retry calls `probe_now` and `reload` |
| `test_countdown_cancel_sends_nothing` | `test_hx_ui_send.py` | invoke `cancel_button` at once; pump until `page == "choose"`; "Cancelled. Nothing was sent."; the fake printer received no write |
| `test_cancel_disappears_at_go` | `test_hx_ui_send.py` | with a scaled countdown, `cancel_button` is gone and `page == "working"` as soon as the whole-send `SendEvent(step=STEP_LOCK, status="ok")` is pumped, before any `STEP_CHECK_MACHINE` event |
| `test_unticked_machine_warning` | `test_hx_ui_send.py` | Line 1 with two machines on the board, both printing "Product of USA"; untick Bagger 2 and choose countries USA and Canada: Bagger 2's warning row shows with its origin in a `BagText`, `send_button` is disabled until `two_origins_var` is ticked; with the same origin (USA) no warning shows; with Bagger 2 `off` no warning shows |
| `test_failure_window` | `test_hx_ui_send.py` | `FakePrinter.web_reply_hook` corrupts the idle-slot read-back: a full-screen `FailureWindow` opens with one row at `STEP_VERIFY_IDLE`; `close()` tells "Choose Try again or Leave it." and stays; Leave it (ask → True) calls `leave_it` and the expected state has `left_by` |
| `test_try_again_restricts_plan` | `test_hx_ui_send.py` | Try again opens a `SendWindow` already running a plan with only the failed machines and a new send_id; the red window is withdrawn, not destroyed, until the retry's "go" event, then destroyed; a retry that ends with `WorkFailed` during its countdown (templates changed) shows the red window again with the message; with another Send window open on its choice page, Try again asks "Replace the choices..." and reuses it (no second `SendWindow`) |
| `test_something_wrong_flow` | `test_hx_ui_send.py` | after a good Send, "Something's wrong" records the rejection, then `StopDialog(reason="rejected")` opens; Stop printing → done text with `fix_button`; Fix and send again opens a Send window prefilled with the same lot and countries |
| `test_stop_failure_goes_red` | `test_hx_ui_send.py` | the fake goes silent before Stop: the dialog's `state == "failed"`, full screen, with Try again |
| `test_resume_dialog_shows_picture` | `test_hx_ui_send.py` | `check_resume` results fill `pictures`/`origins`; a refused machine shows its message; only allowed machines are resumed |
| `test_result_from_log` | `test_hx_ui_send.py` | after a Send, closing the result window and `nav.open_result(send_id)` refills it from the log with the pending machine; for another identity the buttons are absent and "Only {user} can confirm this Send." shows |
| `test_result_from_log_no_entry` | `test_hx_ui_send.py` | with the `send` entry removed from the share and the queue, `nav.open_result(send_id)` lists the live machine from its expected state with its `read_preview` picture and the note "This PC can't find the full record of this Send. These are the printers' latest pictures."; the sender's "Everything is right" confirms it; another identity sees no buttons; a second active machine not in the Send whose expected.json has a newer `format_version` doesn't stop it |
| `test_line_board` | `test_hx_ui.py` | a hand-made board renders one card per line and one row per machine in order; origins in `BagText`; "(origin unknown)" for `""`; machine buttons follow `can_stop`, `can_resume`, `needs_send`, `confirm_pending_by` (same user only) and roles; BLUE rows have no buttons; with a `FakeClock` advanced 3 s, `age_label` reads 3 s more |
| `test_row_closes_up_when_alerts_end` | `test_hx_ui.py` | a machine row whose alerts end is as tall as one that never had any: `alerts_frame` (like the details and the buttons) is unpacked when empty, and details, alerts and buttons keep that order |
| `test_floor_layout` | `test_hx_ui.py` | `floor_layout(1920, 1080, 12)` gives `header_h` 76, `origin_px` 42, `status_px` 25; with 2 rows `row_h` is 251.0; with 20 rows `row_h` is 50.2; 12 machines on 4 lines give `row_h` 71.71 and fit; 30 machines on 5 lines at 1280x800 give 44 px rows, `scrolls` and the rows narrower by the scrollbar; `fit_px`, `ellipsize` and `short_name` as §10.3 says |
| `test_floor_board` | `test_hx_ui.py` | rows in board order under their lines' bands; names without the line's name; `origin_items` texts equal the rows' origins; a NOT MONITORED row's texts are `DIM_FG`; the colour bar alternates while `unacked_sound`; `size="1280x800"` gives that geometry; `monitors()` patched to `[]` uses the Tk screen; the Acknowledge button shows only with `roles.acknowledge` |
| `test_rows_fit_without_overlap` | `test_hx_ui.py` | at 1280x800 and 1920x1080, 6 machines on 2 lines and 12 on 4: no scrolling, the last row on screen, every text inside its column and row with `width` 0, one size per column, and the origin size the largest at which the widest origin fits |
| `test_one_origin_size_never_cut_short`, `test_long_names_and_statuses_get_an_ellipsis`, `test_scrolls_when_the_rows_cannot_fit` | `test_hx_ui.py` | a very long origin shrinks every origin and is drawn whole; long names and statuses end in "…", origins never; 30 machines scroll with the wheel, a new board keeps the place, and a board that fits hides the scrollbar |
| `test_alert_sounder` | `test_hx_ui.py` | `hx_win.play_alert`/`stop_alert` patched: a board with a sound alert to `user:EXAMPLE\jsmith` plays once (a second board doesn't replay); an acknowledged board stops it; a `"floor"` alert rings only while the floor board is open; the `AlertPopup` opens for the personal alert only |
| `test_alert_wav` | `test_hx_ui.py` | `assets/alert.wav` matches `alert_wav_bytes()` within 1 per sample; 22,050 Hz, mono, 16-bit, 66,150 frames |
| `test_admin_save_and_conflict` | `test_hx_ui_admin.py` | change a line's name and save: revision + 1 and "Saved Line 1." ; after another store writes a newer revision, Save shows the `RevisionConflict` text and `reload_button` |
| `test_admin_refuses_non_admin` | `test_hx_ui_admin.py` | `nav.open_admin()` for a sender only tells "Only Stripes Administrators can do this." |
| `test_printer_password_once` | `test_hx_ui_admin.py` | a new printer with password "123321": the saved token unscrambles to `make_token("123321")`; the password entry is empty after Save; neither "123321", its md5 hex nor the plain token appears in the record file or in any `hx-admin` log file; the status reads "A password is saved..." |
| `test_film_files_and_previews` | `test_hx_ui_admin.py` | with `filedialog.askopenfilename` patched to a generated JPEG: `save_file` and `save_preview` ran, the `FileRef` is in `working.files`, the thumbnail shows, a second front photo replaces the first ref; a PDF gets no preview |
| `test_print_text_membership` | `test_hx_ui_admin.py` | ticking a film in the print-text designer and saving adds the id to the film's `print_text_ids`; unticking a film's default is refused with its problem and nothing is saved |
| `test_longest_values_preview` | `test_hx_ui_admin.py` | for the storage.md print text (gap 4 on the lot) and the `FakeWorld` countries, `preview_lines` gives `["Packed on 10/29/26", "Product of USA, Canada, and Mexico Lot 88888888"]` for 29 Oct 2026 |
| `test_origin_preview_matches_plan` | `test_hx_ui_send.py` | `origin_preview` equals `merge_plan(...).machines[0].origin_line` for a normal and a pre-printed film |
| `test_origin_rule_ok_matches_validate_film` | `test_hx_ui_admin.py` | for every film/print-text pair of a small world, `origin_rule_ok` is False exactly when `validate_film` reports the origin-rule problem |
| `test_test_print_window` | `test_hx_ui_admin.py` | estimate text for a 5-step delay range; Start (ask → True) runs the test on the fake; pump until finished; "Printing is off and slots 9 and 10 are empty..."; the fake's slots 9 and 10 are empty and printing is off |
| `test_calibration_refuses_delay_machine` | `test_hx_ui_admin.py` | `CalibrationWindow` for a delay machine tells "Delay machines aren't calibrated." |
| `test_calibration_window` | `test_hx_ui_admin.py` | an encoder world (`{"placement": "encoder"}`) whose machine is first written with `head_distance_mm=None` and no `Calibration`, film length 610: both plans are made and both prints run; the print buttons are disabled until "Save rough distance" (400) has been saved and a `DataUpdate` shows it; `spacing_text` names 365 and 55 and `head_text` 515 (from the plans); printing the head mark on the fake records `printed[-1]["position_mm"] == 515`, and with 317 typed, `head_result` reads 412 mm, with `head_distance_from_print` (patched to record its arguments) called with `sent` equal to that DLYMM; marks A 167 and B 467.5 give "Gap: 300.5 mm." and the within-tolerance text; the mark entries are disabled until each run's `TestFinished`; Save writes the `Calibration` with `head_print_sent_mm` 515 and the machine's `head_distance_mm` goes from None to 412; after the head print, saving rough 450 clears `head_measured_var` and `head_result`, 317 can't be entered until the mark is printed again, and then `head_distance_from_print` receives the `sent` of that new run (465); marks A 167 and B 473 give the wheel text with `{wheel}` 57; after the fake's ENWHD is changed without updating the machine's `encoder_wheel_mm`, the next print ends with the encoder-settings failure |
| `test_test_print_not_stopped` | `test_hx_ui_admin.py` | a `TestFinished` with `printing_off` False turns the test window into the red "Stop it at the printer now" page; its Try again calls `end_again` for an administrator who isn't a sender |
| `test_action_ids` | `test_hx_ui_send.py` | each of the three dialogs starts its thread as `hx-action-<action_id>` with the id it passed to `PrinterActions`, and ignores an `ActionEvent` with another id |
| `test_offline_send_recorded` | `test_hx_ui.py` | a Send with the share down (`no_lock`): `nav.offline_sends` holds its id; after `ShareHealth(ok=True)` `record_offline` runs once (patched pipeline) and the id is dropped |
| `test_user_settings` | `test_hx_ui.py` | defaults; bad values (`"huge"`, `-1`, `"10x10"`) fall back; save then load round-trips |
| `test_bag_text_survives_spanish` | `test_stripes.py` | with a prefilled Send window (trial plan built), a `ResultWindow` of a made-up result and a floor board: every `BagText.get()`, every `itemcget(text_item, "text")` and every floor `origin_items` text is recorded; `i18n.set_language("es")`, `stripes.App._retranslate(root)`, `hx_ui.language_changed(services)`: all identical, while `send_button` now reads "Enviar a 1 máquina"; back to English restores "Send to 1 machine" |
| `test_strict_threads` | `test_hx_ui.py` | `BagText.set` from a worker thread raises `RuntimeError` |
| `test_can_quit` | `test_hx_ui.py` | nothing running: True without asking; `mark_running` then ask → False: False |
| `test_monitor_flag` | `test_hx_ui.py` | `poller.set_monitor_open` patched: opening the MapleJet monitor calls `(True, floor_board=False)`, the floor board `(True, floor_board=True)`, closing both `(False, floor_board=False)` |

`test_i18n.py` changes as architecture §9.2 says; every text of §16 then needs its Spanish.

## 18. OPEN items

| Item | Interface here | Safe default built now |
|---|---|---|
| Encoder calibration procedure (plan; architecture §11) | `CalibrationWindow`, `plan_test(kind="spacing" / "head_distance")` | As §13, following send.md §16: the rough distance saved first, two marks 300 mm apart measured from the bag's edge (A and B), one mark at the position send.md chooses, the head distance from `head_distance_from_print` with the DLYMM, film length and rough distance of the run that printed the mark; the wheel size suggested in whole mm, to be set on the printer and on the machine record; an encoder machine without a head distance can't be sent to, but can be calibrated |
| Which counter counts bags | Company page radio (`bag_counter`); test-print estimate text | `PMCNT`; "none" shows timed steps (`seconds_per_bag × bags_per_step`) |
| Print speed units and range | Machines page "Print speed (cm/min)" | cm/min, 1–2500, no default (empty blocks sending) |
| Beacon | none | no UI at all (`BEACON_SUPPORTED = False`) |
| Film print length | Film editor "Print length (mm)" | empty: the Send window shows "Print length unknown" from `merge_plan` |
| Item gap units; a pixel-accurate designer preview | "Gap" Spinbox 0–`GAP_MAX`; `preview_lines` | a text preview of the longest values, marked approximate; the test print shows the real thing |
| Floor-board readability from the floor | `floor_layout`, `FLOOR_MIN_ROWS` | rows fill the screen, at most a quarter each; origin at half the row height; check on the production-schedule PC and adjust the factors |
| Full-screen windows on the floor PCs' Windows build and screens | `FailureWindow` (`-fullscreen`, `-topmost`), `FloorBoard` (`overrideredirect` plus monitor geometry from `hx_win.monitors()`) | as specified; confirm with two screens connected |
| Loudness of the alert on the floor | `alert_wav_bytes()` amplitude 0.45 | Windows' volume decides the rest |
| Decimal comma in numbers typed by Spanish-speaking admins | `parse_number` | one "," with no "." is read as the decimal point |

## 19. Decisions made in this spec

1. **MapleJet screens translate themselves.** `HxWindow` and `MapleJetTab` are `hx_no_translate`, so
   `App._retranslate` never walks into them; each keeps its fixed text in a `Texts` registry and
   re-renders its `Msg`s in `retranslate()`. A language switch never closes a MapleJet window, so a
   half-filled Send window keeps its choices, and bag text has two guards (its canvases, and the skipped
   containers).
2. **Something's wrong records the rejection at once** (architecture §8), then asks about stopping. "Go
   back" closes the stop question; it doesn't undo the rejection.
3. **Confirmation is per line** in the result window, so a multi-line Send can't stop a good line
   because another is wrong.
4. **A mixed Send opens both windows**: the result window for the machines that went live, and the
   full-screen red window on top for the ones that failed.
5. **The red window can't be closed** except through Try again or Leave it (plan: it "waits for Try
   again or Leave it"); no key closes it.
6. **Try again** opens a Send window that starts the countdown at once with `plan.restricted(...)`.
7. **One window per key**: one Send window, one result and red window per Send, one monitor, one floor
   board, one admin window, one test print per film.
8. **Role-gated widgets are hidden; state-gated ones are disabled** on windows and line cards and left
   out of machine rows.
9. **Admin screens never talk to printers.** Whether a password works shows on the machine's card
   within seconds; test prints and calibration are the only printer work started from admin screens.
10. **The password entry** has no Tk variable, is cleared at Save, and the scrambled token is kept in the
    editor so a failed save can be retried without retyping.
11. **A film has one front and one back photo**: adding another replaces the older reference (the
    original stays on the share). Added files are part of the unsaved edit until the film is saved.
12. **Printer address** must be an IPv4 address (the plan's fixed addresses), unique with its port among
    active printers; names unique ignoring case.
13. **Notify user** is typed as `DOMAIN\name`, with a "Me" button.
14. **Adjustment limits** are ±1000 mm and ±10000 ms.
15. **The longest-value preview is text**, line by line, marked approximate, because the printer's font
    metrics aren't known yet.
16. **Allowed films are edited from both sides**; from the print-text side the print text is saved first,
    then each changed film with its own revision check; any problem saves nothing.
17. **Floor board rows** are at most a quarter of the screen tall; the origin shrinks to 40 % of its size,
    then wraps, and is never cut. Full screen is a borderless window on the chosen monitor, not topmost,
    with a Close button; Escape does nothing.
18. **The floor board opens at startup even when Stripes starts hidden in the tray**, through the logon
    shortcut IT already deploys: no Run key, no installer, the exe stays a single file.
19. **The personal pop-up** shows only alerts addressed to this user; floor alerts show on the floor
    board, and every alert shows on its card.
20. **`alert.wav` is generated by `alert_wav_bytes()`** and committed; the test allows ±1 per sample for
    platform maths.
21. **Long runs use `start_thread`** and report crashes as `WorkFailed`; `STRICT_THREADS` makes off-thread
    widget updates fail in tests.
22. **`TestPrintWindow` only runs `kind="test"`**; calibration has its own window sharing
    `TestRunPanel`, saves the rough distance before either print, and shows the positions send.md's
    plans will send (the admin never types a print position for calibration).
23. **The Zebra tab is selected at start**, and the header subtitle becomes "Zebra labels and MapleJet
    bag printing".
24. **Numbers accept a decimal comma.**
25. **The tab counts as an open monitor only while the main window is viewable**, so a PC hidden in the
    tray doesn't keep standalone polling for a tab nobody sees.
26. **`stop_services` stops the three services in parallel** against one deadline of `timeout_s + 0.5` s.
27. **`can_quit` names the first running item only**, in one question.
28. **Every machine of a line is ticked by default** in the Send window, except one with no printer set
    up.
29. **The Send window shows the origin before the plan merges** (`origin_preview`), and everything else
    from a trial `merge_plan`, so the preview is what will be sent.
30. **Times are shown as `YYYY-MM-DD HH:MM`** (not translated), matching storage.md's texts.
31. **Changing floor-board settings while it is open** closes and reopens it.
32. **Escape closes only small dialogs** (settings, photo, adjustment, ask); nothing that sends, stops or
    confirms has a keyboard shortcut.
33. **Line cards use two columns from 1100 px**, in the fixed board order.
34. **`hx_ui_common.tell`/`ask` are called through the module** everywhere, so one patch covers every
    screen in tests; the `ask` dialog is also marked `hx_window` so the Zebra shortcuts ignore it.
35. **Unticked machines that still print another origin** must be acknowledged with "I know this line
    will print two origins" before the Send button works, and the log records them.
36. **The red window stays (withdrawn) until a Try again is past its countdown**, and comes back if the
    retry fails before its first printer request, so a red line is never left without it.
37. **Try again goes through the one Send window** (`run_send` via the Navigator).
38. **Cancel disappears at the pipeline's "go"** (`SendEvent(STEP_LOCK, "ok")`), the moment the pipeline
    starts ignoring it.
39. **The UI makes each action's id** and names the thread after it.
40. **Sends made without the share** are remembered by the Navigator for the session and recorded when
    the share returns (send.md §14.1), together with any answer the sender gave meanwhile.
41. **A test whose end couldn't confirm printing off** gets the full-screen "Stop it at the printer now"
    page with a Try again the administrator can use (`end_again`).
