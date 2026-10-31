# MapleJet Hx build spec: architecture

Part 1 of 7. This file is the anchor: it fixes the module boundaries, every public name, the shared
data types, the thread rules, the error model, the test set-up and how `stripes.py` hosts the new code.
The other six files fill in behaviour behind these names.

Contents: 0 How to use this spec · 1 Goals and spec index · 2 Glossary · 3 Module map ·
4 hx_core · 5 hx_model · 6 Module interfaces · 7 Threads and concurrency · 8 Error model ·
9 Testing · 10 stripes.py integration and paths · 11 OPEN items · 12 Decisions made in this spec

## 0. How to use this spec

1. Authority, highest first: the agreed plan (product behaviour), this file (names, types, boundaries,
   threads), then protocol.md, storage.md, states.md, send.md, ui.md and deploy.md (behaviour inside
   their own modules).
2. Every public name, signature, field and module boundary in this file is fixed. A spec file may add
   public names only inside the modules it owns (see §1), and may add optional fields only to the
   dataclasses it owns (marked in §5). Nothing may rely on a public name that isn't defined here or in
   the owner's spec.
3. JSON keys are the dataclass field names, snake_case, unless storage.md says otherwise for a
   specific file. storage.md owns the file envelope (format version, counters, completion marker).
4. "OPEN" marks something to confirm on the test unit. A safe default is always given and must be
   built.
5. The repo is public. Examples use 192.0.2.x addresses, UID 1109 with password 123321,
   time.example.local, "Example Co", "Line 1 Bagger 1" and EXAMPLE\jsmith. No real addresses,
   names, UIDs, passwords, tokens, customers or domain names anywhere: docs, code, tests or fixtures.
6. The output stays a single-file `Stripes.exe` built by PyInstaller, signed in CI and deployed exactly
   as today (Group Policy copies it per user). No MSI, no installer, no `--quit` option, no detection
   of old copies.

## 1. Goals and spec index

Goals:

1. Stripes designs, sends and verifies everything MapleJet Hx printers print on bags. Printers only run
   what Stripes puts in message slots 9 and 10.
2. Every Send is read back from the printer three times (idle slot before the switch, running message
   twice after) and confirmed by the sender from the printer's own pictures.
3. Never more than one request in flight to a printer, across the whole plant.
4. A failure stops at the step that failed, shows it loudly and does nothing else by itself. The only
   automatic write is the poller's one clock set after a daylight-saving change.
5. Sending works for 24 hours from a local copy when the share is down.
6. The Zebra side keeps working as it does today. No new runtime dependencies.
7. Parallel agents build the modules against these boundaries, and everything is tested against fake
   printers, a fake share and a fake clock first.

Spec index:

| File | What it covers | Modules it owns |
|---|---|---|
| architecture.md | This file: module map and layering, public interfaces and shared types, threads, errors, test set-up, `stripes.py`/`i18n.py` changes, paths | `hx_core`, `hx_model`, `hx_win` |
| protocol.md | Autopilot and web client: HTTP transport (no proxy, one request per connection), token and web login, Load plus `sme_repeat` with request numbers, the SME reply parser and its fixtures, the query catalogue and reply sense checks (ranges, error codes, "something else is talking" counting), the write whitelist and forbidden queries, retries and the warm-up read, the per-printer request gate, the token scrambling algorithm; the fake printer and fake SNTP server | `hx_protocol`, `hx_fake` |
| storage.md | The v2 folders on the share and on each PC; every JSON file format (records, status files, locks, heartbeats, logs, local copy, queue); whole-file writes and swap-safe reads; the 5 s share timeout and 30 s retry; printer locks (create, refresh, stale takeover); heartbeats; poller assignment, takeover and handover; the local copy and the 24-hour rule; the send-log queue; admin-change log entries; fault injection for the fake share | `hx_share` |
| states.md | The per-machine state machine (codes, colours, transitions, second readings); the poller cycle (what is read when, request budget, backoff, daily checks, standalone mode, when the automatic clock set fires); line aggregation; alerts (kinds, recipients, sound); acknowledgements; the status watcher; floor-board content and order | `hx_state`, `hx_poller` |
| send.md | The Send pipeline step by step; plan merge and validation; message build (items, placement, date codes, print settings); message names and content codes; compare and fingerprint; print position and delay; failure flows (red window data, Try again, Leave it, PC crash); Stop, Resume and Set clocks; test print and calibration; time (SNTP, the TD command, midnight rollover, daylight-saving detection); origin-wording rules; send-log and event-log contents | `hx_message`, `hx_time`, `hx_send` |
| ui.md | The Zebra and MapleJet tabs; every MapleJet screen (Send, result and confirm, red window, Stop/Resume/Set clocks, monitor, floor board, admin screens, film editor, print-text designer, test print and calibration windows); per-user settings; i18n texts and their Spanish; bag-text widgets; shortcuts; the alert sound | `hx_ui`, `hx_ui_common`, `hx_ui_send`, `hx_ui_monitor`, `hx_ui_admin` |
| deploy.md | Build and release: `pyinstaller.spec`, `make_version.py` and the scrambling-key injection (`hx_key.py`), CI (tests on `windows-latest`; Linux under xvfb is the developers' run), build, self-check, signing, release, the single-file `Stripes.exe` (no MSI); the self-check list; the three `stripes.py` key hooks (§10.6 item 5); the folder-script spec (v2 folders, permissions, one-time Zebra copy); the test-PC instructions; README updates | `pyinstaller.spec`, `make_version.py`, `test_make_version.py`, `test_deploy.py`, `.github/workflows/build.yml`, `build.bat`, `.gitignore`, `README.md`, the generated `hx_key.py`; delivered after development: `tools/New-StripesV2Folders.ps1`, `test_folder_script.py`, `docs/maplejet/test-pc.md` |

## 2. Glossary

| Term | Meaning |
|---|---|
| Printer | One Hx printer: address, UID, scrambled token. Record `Printer`. |
| Machine | One bagger with one printer. Record `Machine`. A printer belongs to at most one active machine. |
| Line | A group of machines with one notify user and one cartridge type. Record `Line`. |
| Slot | A stored message number 1–10 on the printer. Stripes writes only 9 and 10. |
| Idle slot | Whichever of 9 and 10 isn't printing; 9 if neither is. |
| Send | Building, checking and switching a new message on each chosen machine. |
| Read-back | The printer's own copy of a message from the web Load request: `MessageDesign`. |
| Bag text | Anything that prints on a bag, including the origin line. English only; never translated. |
| Origin line | "Product of USA and Canada", or "USA (pre-printed)" for a pre-printed film. |
| Poller | The background service on each PC that polls the printers assigned to it. |
| Assigned poller | The one PC (heartbeat key) currently allowed to poll a printer. |
| Printer lock | A file in `status\locks` that a Send, test print, Stop, Resume or clock set holds. |
| Expected state | `status\machines\<id>.expected.json`: what senders and testers say should be running. |
| Poll status | `status\machines\<id>.poll.json`: what the assigned poller last read and judged. |
| Fresh / stale | A status file is fresh while its counter keeps changing; stale when it hasn't changed for the set time on the reader's own monotonic clock. |
| Local copy | Each user's copy of the Hx config and the active templates, for when the share is down. |
| Standalone mode | Share unreachable: only PCs with a MapleJet monitor open poll, every 30 s, quick items only. |

## 3. Module map

All new modules are flat files at the repo root, prefixed `hx_`. Pure modules do no file, network,
Windows or Tk work. "stdlib" below means the standard library minus `ssl`, `_hashlib` and
`multiprocessing` (excluded from the exe); `hashlib.md5`/`sha256` work through CPython's built-ins.
`i18n` (no dependencies) may be imported by every module, but only for `N_()` and `add_spanish()`
outside the UI modules (see §5.2).

| Layer | Module | Responsibility | Owner | May import (besides stdlib and `i18n`) |
|---|---|---|---|---|
| 0 | `hx_core` | Clock, fake clock, cancel token, event bus, `Msg`, `Cancelled`. Pure | architecture.md | nothing |
| 1 | `hx_model` | Constants, `Timings`, exceptions, JSON mapping, `Identity`, every record, plan, command, read-back, status, result, event and view dataclass. Pure | architecture.md | `hx_core` |
| 2 | `hx_message` | Plan merge and validation, origin wording, print position and delay, message name and content code, `build_commands`, `compare`, fingerprint, clock command, test steps, calibration maths. Pure | send.md | `hx_core`, `hx_model` |
| 2 | `hx_state` | Machine state machine, line aggregation, alerts, views, floor-board rows, which alerts sound here. Pure | states.md | `hx_core`, `hx_model`, `hx_message` |
| 2 | `hx_win` | Every Windows call through ctypes or `winsound`, each with a non-Windows fallback: identity and group SIDs, swap-safe file open, monitors, alert sound | architecture.md (§6.3) | `hx_core`, `hx_model` |
| 3 | `hx_protocol` | `PrinterClient`, `ClientPool`, request gate, token, scrambling, SME parser, query catalogue, write whitelist | protocol.md | `hx_core`, `hx_model` |
| 3 | `hx_time` | SNTP client, `TimeSource`, local time conversion, `recent_dst_change` | send.md | `hx_core`, `hx_model` |
| 3 | `hx_share` | Paths, `ShareStore`, `FileOps`, `PrinterLock`, `Heartbeat`, `CounterWatch`, assignment formula, `LocalStore`, `DataService`, logs and queue | storage.md | `hx_core`, `hx_model`, `hx_win` |
| 4 | `hx_send` | `SendPipeline`, `PrinterActions` (Stop, Resume, Set clocks), `TestPrintRun` | send.md | layers 0–3 except `hx_state` |
| 4 | `hx_poller` | `Poller` (coordinator plus per-printer workers, heartbeat, assignment), `StatusWatcher` | states.md | layers 0–3 (incl. `hx_state`) |
| 5 | `hx_ui_common` | Shared Tk widgets and helpers: `BagText`, `PictureView`, `HxWindow`, colours, dialogs, `HxSettings`, `SyncBackground` | ui.md | layers 0–4, `tkinter`, `PIL` |
| 6 | `hx_ui_send` | Send window, result/confirm window, red failure window, Stop/Resume/Set clocks dialogs | ui.md | layers 0–5 |
| 6 | `hx_ui_monitor` | Line board cards, MapleJet monitor window, floor board, `AlertSounder` | ui.md | layers 0–5 |
| 6 | `hx_ui_admin` | Admin window and pages, film editor, print-text designer, test print and calibration windows | ui.md | layers 0–5 |
| 7 | `hx_ui` | `HxServices`, `make_services`/`start_services`/`stop_services`, `MapleJetTab`, `Navigator`, startup, `self_check` | ui.md | layers 0–6 |
| 8 | `stripes.py` | Hosts both tabs; the hooks in §10 | architecture.md §10 (deploy.md §1.3 for the key hooks) | `hx_ui`, `hx_share` (paths only), `hx_key` (only to set `HX_KEYED`, §10.6 item 5) |
| test | `hx_fake` | `FakePrinter`, `FakeSntp`, `FakeWorld`; also runnable as a script for manual UI work | protocol.md | layers 0–3, `http.server`, `PIL` |
| build | `hx_key` | Generated at build time, git-ignored: `KEY = b"..."`. Absent in source checkouts | deploy.md | nothing |

Rules:

1. A module imports only from lower layers. Same-layer imports are only those listed (`hx_state` uses
   `hx_message.fingerprint`, `parse_message_name` and `NOTE_TEXTS`). The three layer-6 UI modules
   never import each other; they reach each other's screens through `HxServices.nav` (§6.10).
2. No module below layer 5 imports `tkinter` or `PIL`. No module imports `stripes`.
3. Application code never imports `hx_fake`. It is test-only and isn't bundled (nothing imports it).
4. Every printer request goes through `hx_protocol.PrinterClient`. Every share or PC-folder file access
   for hx data goes through `hx_share`. Every Windows API goes through `hx_win`.
5. Each module starts with a docstring like the existing files, and keeps plain, short comments.

Other files:

| Path | What | Owner |
|---|---|---|
| `docs/maplejet/*.md` | This spec | each author |
| `testdata/hx/` | Sanitized captures used as parser fixtures (UID 1109, host 192.0.2.10, tokens removed) | protocol.md |
| `assets/alert.wav` | The alert sound, bundled with the existing `assets` folder | ui.md (deploy.md checks it's bundled) |
| `i18n.py` | One new function, `add_spanish` (§10.6), and the Spanish for the few new `stripes.py` texts (this file's, ui.md §1.6's and deploy.md §1.5's) | architecture.md (written by WP1) |
| `test_hx_*.py` | Tests, one file per module (the five UI modules share three files, ui.md §17) plus `test_hx_sim.py` (§9; written by WP6) | each module's owner |
| `test_stripes.py`, `test_i18n.py` | Changed (§9.2) | `test_stripes.py`: architecture.md, with ui.md §17.2's two tests (written by WP9); `test_i18n.py`: architecture.md (written by WP1) |

The spec files say what each file must do; the work packages in README.md "Work packages" say which
agent writes it. Each file has one writer package, so `stripes.py` is written only by WP9 and
`i18n.py` only by WP1, whichever spec file describes a change to them. The packages change no name or
boundary here.

## 4. hx_core (complete specification)

Pure, stdlib only. Everything with time in it takes a `Clock`, so tests can replace it.

```python
class Clock:
    """Wall time, monotonic time and sleeping. Every hx_ timer goes through one."""
    def time(self) -> float: ...        # this PC's wall clock, epoch seconds (like time.time())
    def monotonic(self) -> float: ...   # every interval, timeout and staleness check uses this
    def now(self) -> datetime: ...      # local naive datetime of self.time(), via time.localtime
    def sleep(self, seconds: float, cancel: "CancelToken | None" = None) -> bool: ...
        # True when the time passed; False as soon as cancel fires (returns at once if already cancelled)

class SystemClock(Clock): ...           # time.time, time.monotonic, cancel's Event.wait or time.sleep
SYSTEM_CLOCK = SystemClock()

class FakeClock(Clock):
    def __init__(self, start: float = 1_793_275_200.0, *, auto_advance: bool = True): ...
        # start = 29 Oct 2026 12:00 UTC; monotonic() starts at 1000.0
    def advance(self, seconds: float) -> None: ...   # moves time() and monotonic() together, wakes due sleepers
    def set_wall(self, epoch: float) -> None: ...    # moves time() only: a PC clock jump
    def sleepers(self) -> int: ...                   # threads blocked in sleep() right now (manual mode)
```

- `auto_advance=True`: `sleep(s)` adds `s` to both clocks and returns `True` straight away (or `False`
  if `cancel` is already set). For single-threaded logic tests.
- `auto_advance=False`: `sleep(s)` blocks until `advance()` passes its deadline (`True`) or `cancel`
  fires (`False`). If nothing advances it for 10 real seconds it raises
  `RuntimeError("FakeClock: nobody advanced the clock")`, so a broken test fails instead of hanging.
- `now()` converts with `time.localtime`, so tests that depend on the time zone pass an offset
  function to `hx_time` instead (§6.5).

```python
class Cancelled(Exception):
    """Raised by CancelToken.check() once cancel() has been called."""

class CancelToken:
    def cancel(self) -> None: ...
    @property
    def cancelled(self) -> bool: ...
    def check(self) -> None: ...                     # raises Cancelled if cancelled
    def wait(self, seconds: float) -> bool: ...      # real-time wait; True if cancelled meanwhile
    event: threading.Event                           # used by Clock.sleep implementations
```

```python
@dataclass(frozen=True)
class Msg:
    """A piece of UI or log text kept in English: text is marked with N_() where the Msg is made,
    values are filled in by tr() in the UI (or by english() in logs). Values must never contain
    bag text: bag text is shown in its own widgets."""
    text: str
    values: dict = field(default_factory=dict)       # str, int, float or a nested Msg
    def english(self) -> str: ...                    # self.text.format(**values), nested Msgs in English
    def to_json(self) -> dict: ...                   # {"text": ..., "values": {...}}; nested Msgs as dicts
    @staticmethod
    def from_json(d: dict) -> "Msg": ...             # a value that is a dict with "text" becomes a Msg
```

Plain values (names, numbers, user names) are inserted as they are. A value that is itself a fixed
English word the reader should see translated, such as an ink colour, is passed as a nested
`Msg(N_("white"))`; the UI translates nested values first. ui.md's helper is
`hx_ui_common.text(msg) -> str`.

```python
class EventBus:
    """Worker threads post events; the Tk thread pumps them to subscribers. Never blocks a poster."""
    def post(self, event: object) -> None: ...       # any thread; queue.SimpleQueue underneath
    def subscribe(self, event_type: type, callback: Callable[[object], None]) -> int: ...
        # callback(event) for events that are isinstance(event, event_type); returns a token
    def unsubscribe(self, token: int) -> None: ...   # safe to call from inside a callback
    def pump(self, max_events: int = 1000) -> int: ...
        # Tk thread only: deliver up to max_events queued events in order; returns how many.
        # A callback that raises is reported with traceback.print_exc() and the pump carries on.
```

- In the app, `hx_ui` calls `bus.pump()` from `root.after(100, ...)`, the same 100 ms cadence as
  `stripes.Background`. Tests call `pump()` directly; no Tk needed.
- `subscribe`/`unsubscribe` are Tk-thread only. `post` is the only cross-thread entry point.

## 5. hx_model

Pure data. Conventions in the blocks below: every dataclass is `@dataclass(kw_only=True)` unless shown
otherwise; `= []` and `= {}` mean `field(default_factory=list)` / `field(default_factory=dict)`; times
are epoch seconds (`float`) from the writer's own `Clock.time()` and are for display and logs only.
Staleness is never judged from another PC's timestamps (§7.4). The module uses
`from __future__ import annotations`, so the order of the blocks doesn't matter, and `from_json`
resolves types with `typing.get_type_hints`.

### 5.1 Constants and timings

```python
FORMAT = 1                     # format version written into every v2 file (storage.md)
SLOTS = (9, 10)                # the only slots Stripes writes
DEFAULT_IDLE_SLOT = 9          # the idle slot when neither 9 nor 10 is printing
MESSAGE_NAME_MAX = 20
CODE_MAX = 3                   # line, machine and print-text codes
FILM_CODE_MAX = 6              # send.md may lower this to fit the name format, never raise it
FONT_INDEXES = range(1, 7)     # FONTIND 1..6
SIZE_RANGE = range(1, 11)      # SIZE as sent; read back one lower (0..9)
BOLD_RANGE = WIDTH_RANGE = range(1, 11)
RESOLUTIONS = (0, 1, 2, 3)     # RESMOD: UltraFine, Fine, Normal, Draft
DELAY_US_MAX = 120_000_000     # DELAY and DLYMM upper limits from the manual
DLYMM_MAX = 120_000_000
CANVAS = (256, 160)            # message canvas W x H
PREVIEW_SIZE = (1024, 160)     # the read-back picture
STANDARD_FONTS = ("CourierN", "Arial", "ArialNarrow", "ArialNarrowB", "TimesNewRom", "LinearBeam")
    # the default Company.standard_fonts, in printer order (index 1..6). OPEN: exact spelling as read
ALLOWED_WRITE_TAGS = ("CRTMSG", "CTEXT", "CTDAT", "CPPARM", "SAVEMSG", "PRINTMSG", "PRINT", "TD")
    # the only write commands Stripes ever sends (whitelist; protocol.md enforces, tests assert)
FORBIDDEN_QUERY_TAGS = ("CPPARM", "ODPARAM", "RS232")   # multi-value queries that hang ~4 s
BEACON_SUPPORTED = False       # OPEN: no beacon query or command is used (BEAC returns -1)

GREEN, AMBER, RED, GREY, BLUE = "green", "amber", "red", "grey", "blue"
COLOURS = (GREEN, AMBER, RED, GREY, BLUE)   # BLUE = a Send or test in progress

# Send steps, in order (send.md defines each). Failures at or after STEP_SWITCH are "after the switch".
STEP_CHECK_CHOICE = "check_choice"   # sender and choice
STEP_LOCK = "lock"                   # countdown and printer locks
STEP_WAIT_POLLER = "wait_poller"     # poller paused for the lock, or stale
STEP_CHECK_MACHINE = "check_machine" # UID, fonts, ink, printing slot
STEP_SET_CLOCK = "set_clock"
STEP_BUILD = "build"                 # CRTMSG .. SAVEMSG into the idle slot
STEP_VERIFY_IDLE = "verify_idle"     # read-back 1: idle slot vs what was sent
STEP_SWITCH = "switch"               # switch pending recorded, PRINTMSG, PRINT 1
STEP_CONFIRM_1 = "confirm_1"         # read-back 2: running message
STEP_CONFIRM_2 = "confirm_2"         # read-back 3: running message again
STEP_OPERATOR = "operator_confirm"   # the sender's "Everything is right" / "Something's wrong"
STEPS = (STEP_CHECK_CHOICE, STEP_LOCK, STEP_WAIT_POLLER, STEP_CHECK_MACHINE, STEP_SET_CLOCK,
         STEP_BUILD, STEP_VERIFY_IDLE, STEP_SWITCH, STEP_CONFIRM_1, STEP_CONFIRM_2, STEP_OPERATOR)
AFTER_SWITCH_STEPS = (STEP_SWITCH, STEP_CONFIRM_1, STEP_CONFIRM_2, STEP_OPERATOR)

ITEM_KINDS = ("fixed", "packed_date", "best_by_date", "lot", "origin", "free_text",
              "machine_code", "line_code")
FILM_STATUSES = ("development", "active", "inactive")     # also used by print texts
CARTRIDGES = ("paper", "plastic")
INK_COLOURS = ("white", "black")
PLACEMENTS = ("encoder", "delay")
LOCK_PURPOSES = ("send", "test", "calibration", "stop", "resume", "clock")
LOG_KINDS = ("hx-sends", "hx-events", "hx-admin")
```

All timing numbers live in one frozen dataclass so tests can run the same code faster
(`TIMINGS.scaled(0.05)`) instead of patching constants. Every class that waits takes
`timings: Timings = TIMINGS`.

```python
@dataclass(frozen=True)
class Timings:
    http_timeout_s: float = 2.0         # every printer request
    read_retry_s: float = 0.5           # the one automatic re-ask of a read ("within a second")
    idle_warmup_s: float = 3.0          # a write after this much quiet is preceded by a warm-up read
    gate_wait_s: float = 30.0           # longest wait for the in-process per-printer gate
    web_repeat_max: int = 5             # sme_repeat attempts per Load
    web_repeat_window_s: float = 3.0    # ... or this long, whichever comes first
    poll_fast_s: float = 5.0            # printing on/off, message name, print counter
    poll_slow_s: float = 60.0           # ink, head temp, date, time, running-message Load
    second_reading_s: float = 1.0       # the confirming reading before a state change or alert
    stale_s: float = 15.0               # status counters and heartbeats
    takeover_s: tuple = (15.0, 20.0)    # poller rank 1 and rank 2 take over after this staleness
    not_monitored_s: float = 30.0       # line greyed out as NOT MONITORED
    lock_stale_s: float = 60.0          # printer lock abandoned when its counter is unchanged this long
    lock_refresh_s: float = 5.0         # a holder bumps its lock counter this often
    heartbeat_s: float = 5.0
    printer_reach_window_s: float = 86_400.0   # a PC gets printers only if it reached one this recently
    share_timeout_s: float = 5.0        # a share operation gives up after this
    share_retry_s: float = 30.0         # an unreachable share is retried this often
    data_refresh_s: float = 60.0        # templates and config reloaded from the share
    local_copy_max_age_s: float = 86_400.0     # Send works from the local copy this long
    standalone_poll_s: float = 30.0     # share down: monitor PCs poll this often, random offset
    refresh_now_min_s: float = 10.0     # "Refresh now" at most this often per PC
    backoff_s: tuple = (5.0, 10.0, 30.0, 60.0)  # an unreachable printer is retried after these
    unreachable_after: int = 3          # failures in a row before a printer counts as unreachable
    countdown_s: float = 5.0            # the Send window countdown
    confirm_gap_s: float = 2.0          # between the two running-message reads (send.md may tune)
    clock_warn_s: float = 120.0         # printer clock this far off raises a warning
    test_no_print_s: float = 60.0       # test print: printing off after this long without a print
    watch_s: float = 5.0                # status watcher reads the status folder this often
    silent_window_s: float = 300.0      # "printing bags in the last few minutes" (states.md may tune)
    def scaled(self, factor: float) -> "Timings": ...
        # every *_s field multiplied by factor (tuples element by element); counts unchanged

TIMINGS = Timings()
```

### 5.2 Exceptions

Every failure the hx modules expect is one of these. Each carries a `Msg` for the operator and,
when known, the Send step. Non-UI modules never call `tr()`; they raise or return `Msg`s.

```python
class HxError(Exception):
    def __init__(self, msg: Msg, *, step: str = "") -> None: ...
    msg: Msg            # what to tell the operator (English, N_-marked)
    step: str           # a STEP_* constant when raised inside a Send, else ""
    # str(e) == e.msg.english()

class PrinterError(HxError): ...          # talking to a printer failed
class NoReply(PrinterError): ...          # timeout, refused, or connection closed without a reply
class PrinterBusy(PrinterError): ...      # reply 7 (busy) or 6 (timeout), or the in-process gate timed out
class AuthFailed(PrinterError): ...       # 401 after a fresh login, or the token can't be unscrambled
class BadReply(PrinterError):             # failed a sense check: out of range, an error code, wrong shape
    tag: str; reply: str
class CommandRefused(PrinterError):       # a write answered something other than "0"
    tag: str; code: int
class WrongPrinter(PrinterError):         # UID differs from the record
    expected: int; got: str
class WebReadFailed(PrinterError): ...    # Load never got its own TN/XID in time, or the reply didn't parse
class ForbiddenCommand(HxError):          # programming error: a non-whitelisted write or a forbidden query.
    tag: str                              # Never caught by hx code; tests assert it can't happen.

class ShareError(HxError): ...
class ShareUnavailable(ShareError): ...   # the share didn't answer within share_timeout_s, or is marked down
class LockHeld(ShareError):               # another live holder has the printer lock
    info: LockInfo
class LockLost(ShareError): ...           # our lock file now names someone else
class FormatTooNew(ShareError):           # a file written by a newer Stripes: "Update Stripes"
    path: str; found: int
class RevisionConflict(ShareError):       # admin save: the record changed since it was loaded
    current: int

class PlanError(HxError):                 # the choice can't be sent; nothing has been written
    problems: list[Msg]                   # every problem found (msg = the first)
class CheckFailed(HxError):               # a read-back differs from what was sent
    differences: list[Difference]
class NotAllowed(HxError): ...            # wrong group, or not the person who sent it
class TimeServerError(HxError): ...       # SNTP got no valid answer (TimeSource catches it)
```

Subclasses with extra attributes take them as keyword arguments after the message, for example
`BadReply(msg, tag="PRIN", reply="7", step=STEP_CHECK_MACHINE)` or `PlanError(problems[0],
problems=problems)`.

### 5.3 JSON mapping

```python
def to_json(obj) -> dict: ...                 # any dataclass here, recursively; tuples become lists; Msg via Msg.to_json
def from_json(cls: type, d: dict): ...        # builds cls from a dict, recursively by type hints;
                                              # unknown keys are ignored, missing keys take defaults;
                                              # raises ValueError for a wrong type (the rules below)
                                              # or a missing required field
def new_id(prefix: str) -> str: ...           # f"{prefix}_{secrets.token_hex(5)}", e.g. "fm_3f9a1c22b0"
def adjustment_id(machine_id: str, film_id: str) -> str: ...   # f"{machine_id}__{film_id}"
def same_user(a: str, b: str) -> bool: ...    # Windows names compare case-insensitively
```

`from_json` type rules (every module's files and tests rely on them, so they are fixed here):

1. An int is accepted for a `float` field and converted to float (JSON writes `610` for `610.0`, and
   records written by hand or by `FakeWorld` hold ints). An int too big for a float (`json.loads`
   takes a 400-digit int, where `float()` raises `OverflowError`), NaN and infinity are refused
   under rule 8: `encode_json` never writes them, so they mean a damaged or hand-edited file.
2. A bool is rejected for `int` and `float` fields (`True` isn't 1 here); an `int` field accepts only
   an int (not a float, not a string).
3. A `str` field accepts only a str.
4. A JSON list is accepted for a `tuple[...]` hint and converted to a tuple, each element checked by
   its hint (a bare `tuple` hint, as in `Timings`, converts without checks). `to_json` writes tuples
   as lists, so the `(font, size)` pairs of `TestStep.fonts` and the `(tag, text)` pairs of
   `DesignItem.pairs` come back as tuples inside their lists. `list[X]` elements and `dict[str, X]`
   values are checked by `X`. JSON object keys are always strings, and `dict[str, X]` keeps them as
   strings.
5. None is accepted only for `X | None` hints (and for a missing field with a default of None).
6. A dict becomes a nested dataclass by its hint (recursively, with these rules); a `Msg` hint uses
   `Msg.from_json`.
7. Fields typed plain `dict` or `list` (no element type) keep any JSON value as it is.
8. Anything else raises `ValueError` naming the class and the field.

`test_hx_model.py` checks each rule, including `from_json(Film, {..., "length_mm": 610})` giving
`610.0`, `from_json(Film, {..., "length_mm": True})` raising, and `TestStep.fonts` and
`DesignItem.pairs` round-tripping with tuple elements.

ID prefixes: `ln` line, `mc` machine, `pr` printer, `pd` product, `fm` film, `pt` print text, `sd` a
Send, `tp` a test print or calibration run, `ac` a Stop, Resume or Set clocks action. The company
record's id is always `"company"`. IDs never change and are used in every file name, status
file and log; names are display only. Records are never deleted (§12, decision 9).

### 5.4 Identity

```python
@dataclass(frozen=True, kw_only=True)
class Identity:
    user: str                    # "EXAMPLE\\jsmith", as Windows reports it (NameSamCompatible)
    computer: str                # COMPUTERNAME
    sids: frozenset[str]         # enabled group SIDs from the sign-in token
    pid: int
    @property
    def key(self) -> str: ...    # heartbeat key: f"{computer}_{user}_{pid}" with every run of
                                 # characters outside [A-Za-z0-9._-] replaced by "_"
    def is_admin(self, company: "Company") -> bool: ...   # company.admin_group_sid in sids
    def is_sender(self, company: "Company") -> bool: ...  # company.sender_group_sid in sids
```

Only Stripes MapleJet Senders send, stop, resume and set clocks. Stripes Administrators edit, publish
and run test prints and calibration. Both may acknowledge alerts. A person who needs both is put in
both groups. An empty group SID in the company settings means nobody is in that group; the folder
script writes the first `company.json` with both SIDs (deploy.md), so there is no chicken-and-egg
problem for the first administrator.

### 5.5 Records

Every stored record has the base fields. `revision` goes up by one on every save (it is the film's
"settings revision" and the print text's "revision").

```python
@dataclass(kw_only=True)
class Record:
    id: str
    revision: int = 0
    updated_by: str = ""
    updated_at: float | None = None

@dataclass(kw_only=True)
class Country:
    name: str                    # exactly as printed: "USA"
    aliases: list[str] = []      # names and abbreviations refused in other text: "US", "U.S.A.", "United States"
    active: bool = True

@dataclass(kw_only=True)
class Ink:
    name: str                    # exactly as the ink-name query reports it
    cartridge: str               # CARTRIDGES
    colour: str                  # INK_COLOURS

@dataclass(kw_only=True)
class Company(Record):           # id "company"
    time_server: str = ""                   # "time.example.local"
    time_server_fallback: str = ""          # "192.0.2.123"
    countries: list[Country] = []
    inks: list[Ink] = []
    lot_min_digits: int = 4
    lot_max_digits: int = 8
    free_text_max: int = 30
    admin_group: str = "Stripes Administrators"      # display name
    admin_group_sid: str = ""
    sender_group: str = "Stripes MapleJet Senders"
    sender_group_sid: str = ""
    standard_fonts: list[str] = field(default_factory=lambda: list(STANDARD_FONTS))
    bag_counter: str = "PMCNT"   # OPEN: which counter counts bags: "PMCNT", "MSCNT" or "none" (time-based)

@dataclass(kw_only=True)
class Line(Record):
    name: str                    # "Line 1"
    code: str                    # printed code, 1..CODE_MAX characters
    cartridge: str               # CARTRIDGES: only films of this type are offered
    notify_user: str = ""        # "EXAMPLE\\jsmith"
    active: bool = True

@dataclass(kw_only=True)
class Machine(Record):
    name: str                    # "Line 1 Bagger 1"
    code: str                    # printed code, 1..CODE_MAX characters
    line_id: str
    printer_id: str
    placement: str               # PLACEMENTS
    head_distance_mm: float | None = None   # encoder only; set from calibration. None: can't send (OPEN)
    encoder_wheel_mm: float | None = None   # encoder only: expected wheel size, checked daily
    encoder_ppr: int | None = None          # encoder only: expected pulses per turn, checked daily
    print_speed: int | None = None          # PRNSPD as sent. OPEN: units (cm/min assumed) and range;
                                            # None: can't send until an administrator sets it
    rotate: bool = False
    mirror: bool = False
    seconds_per_bag: float = 2.4            # timed test steps when no counter counts bags (OPEN)
    active: bool = True

@dataclass(kw_only=True)
class Calibration(Record):       # id = machine_id; the evidence behind Machine.head_distance_mm
    machine_id: str
    spacing_sent_mm: int = 300
    spacing_measured_mm: float | None = None
    spacing_by: str = ""
    spacing_at: float | None = None
    rough_head_distance_mm: float | None = None    # tape measure, to pick the right bag length
    head_print_sent_mm: int | None = None
    head_print_measured_mm: float | None = None    # measured from the bag's bottom edge
    head_distance_mm: float | None = None          # result; saving copies it to the machine
    head_by: str = ""
    head_at: float | None = None

@dataclass(kw_only=True)
class Printer(Record):
    name: str                    # display: "Printer 1"
    host: str                    # fixed IP address: "192.0.2.10"
    port: int = 80               # always 80 in production; tests point it at a fake printer
    uid: int                     # 1109; checked on every poll and Send
    token: str = ""              # the scrambled token (protocol.md); "" until a password is set
    active: bool = True

@dataclass(kw_only=True)
class Product(Record):
    name: str                    # "Example Co 2lb Onion"
    customer: str = ""           # "Example Co"
    active: bool = True

@dataclass(kw_only=True)
class FileRef:
    kind: str                    # "front_photo" | "back_photo" | "artwork" | "spec" | "other"
    name: str                    # the original file name, for display
    path: str                    # relative to v2\templates, forward slashes
    sha256: str
    size: int
    added_by: str = ""
    added_at: float | None = None

@dataclass(kw_only=True)
class Film(Record):
    product_id: str
    code: str                    # [A-Z0-9], 1..FILM_CODE_MAX, used in the message name
    artwork_revision: str = ""   # "Rev C"
    status: str = "development"  # FILM_STATUSES; only "active" is offered at Send
    files: list[FileRef] = []
    side: str = "back"           # "front" | "back": which side gets the code (an instruction)
    cartridge: str = "plastic"   # CARTRIDGES; must match the line's
    ink_colour: str = "black"    # INK_COLOURS; checked against the printer's ink
    length_mm: float | None = None        # cut-off length; required for encoder machines
    position_mm: float | None = None      # from the bag's bottom edge; required for encoder machines
    delay_ms: float | None = None         # after the trigger; required for delay machines
    print_length_mm: float | None = None  # OPEN: for the film-stop warning; None: no warning possible
    font: int = 2                # index into Company.standard_fonts, FONT_INDEXES
    size: int = 4                # SIZE_RANGE, as sent
    resolution: int = 0          # RESOLUTIONS
    bold: int = 1                # BOLD_RANGE
    width: int = 1               # WIDTH_RANGE
    origin_preprinted: bool = False
    preprinted_origin: str = ""  # bag text, only when origin_preprinted: "USA"
    print_text_ids: list[str] = []        # print texts allowed with this film (the only copy of this list)
    default_print_text_id: str = ""

@dataclass(kw_only=True)
class PrintTextItem:
    id: str                      # unique within its print text: "i1", "i2", ...
    kind: str                    # ITEM_KINDS
    before: str = ""             # bag text before the data; must be "" for "origin"
    after: str = ""              # bag text after the data; must be "" for "origin"
    text: str = ""               # "fixed" only
    date_format: str = ""        # "packed_date"/"best_by_date": a hx_message.DATE_FORMATS key, e.g. "MM/DD/YY"
    offset: int = 0              # "best_by_date" only
    offset_unit: str = "days"    # "days" | "months" | "years"
    label: str = ""              # "free_text": the question shown to the sender
    max_len: int = 0             # "free_text": 1..Company.free_text_max
    required: bool = True        # "free_text"
    placement: str = "right"     # "right" of or "below" the previous item; ignored for the first
    gap: int = 0                 # extra space before the item, in canvas units (OPEN: units)

@dataclass(kw_only=True)
class PrintText(Record):
    name: str                    # "Packed on + origin"
    code: str                    # 1..CODE_MAX characters, used in the message name
    status: str = "development"  # FILM_STATUSES
    items: list[PrintTextItem] = []

@dataclass(kw_only=True)
class MachineFilmAdjustment(Record):    # id = adjustment_id(machine_id, film_id)
    machine_id: str
    film_id: str
    position_mm: float = 0.0     # encoder machines; ignored in a test print
    delay_ms: float = 0.0        # delay machines; ignored in a test print
    # updated_by / updated_at are "who set it and when"

@dataclass(kw_only=True)
class HxData:
    """One consistent set of config and templates, from the share or the local copy. Treated as
    immutable: DataService swaps in a new one, never edits it."""
    company: Company
    lines: dict[str, Line] = {}
    machines: dict[str, Machine] = {}
    printers: dict[str, Printer] = {}
    adjustments: dict[str, MachineFilmAdjustment] = {}   # by adjustment_id
    calibrations: dict[str, Calibration] = {}            # by machine_id
    products: dict[str, Product] = {}
    films: dict[str, Film] = {}
    print_texts: dict[str, PrintText] = {}
    source: str = "share"        # "share" | "local"
    loaded_at: float = 0.0
    reached_at: float | None = None   # when this PC last reached the share (the 24-hour rule)
    def line_machines(self, line_id: str) -> list[Machine]: ...      # active machines, by name
    def adjustment(self, machine_id: str, film_id: str) -> MachineFilmAdjustment | None: ...
```

### 5.6 Send request and plan

```python
@dataclass(kw_only=True)
class LineChoice:
    line_id: str
    machine_ids: list[str]       # the ticked machines of that line
    product_id: str
    film_id: str
    print_text_id: str
    lot: str = ""
    countries: list[str] = []    # Country.name values, in the order picked
    free_text: dict[str, str] = {}   # PrintTextItem.id -> value

@dataclass(kw_only=True)
class SendRequest:
    send_id: str                 # new_id("sd")
    sender: str                  # Identity.user
    computer: str
    created_at: float
    lines: list[LineChoice]

@dataclass(kw_only=True)
class PrintSettings:             # every print setting Stripes sends (the CPPARM of the new message)
    encoder: bool                # ENCST
    position_mm: int = 0         # DLYMM, encoder machines
    delay_us: int = 0            # DELAY, delay machines
    speed: int                   # PRNSPD
    rotate: bool = False         # ROTAT
    mirror: bool = False         # MIRR
    resolution: int = 0          # RESMOD
    bold: int = 1                # BOLD
    width: int = 1               # WDTH
    quantity: int = 1            # QUANT
    invert: bool = False         # REVRS
    auto_repeat: bool = False    # AUTREP

@dataclass(kw_only=True)
class PositionWorking:           # how the position or delay was worked out, for the log and the window
    placement: str               # PLACEMENTS
    film_value: float            # Film.position_mm or Film.delay_ms (or the test step's value)
    adjustment: float            # mm or ms; 0 in a test print
    head_distance_mm: float | None = None
    film_length_mm: float | None = None
    sent: int                    # DLYMM in mm, or DELAY in microseconds
    wrapped: bool = False
    text: str                    # English: "(150 - 412 + 0) mod 610 = 348 mm"
    warning: Msg | None = None   # the film-stop warning

@dataclass(kw_only=True)
class TestStep:
    index: int                   # 1-based
    label: str                   # bag text printed with the step: "P150 | B1" or "D480 | B1"
    position_mm: float | None = None
    delay_ms: float | None = None
    bold: int
    width: int
    resolution: int
    fonts: list[tuple[int, int]] = []    # (font index, size) printed side by side

@dataclass(kw_only=True)
class MachinePlan:
    """Everything needed to build one machine's message, merged from every level. Records are copies."""
    machine: Machine
    line: Line
    printer: Printer
    product: Product | None      # None in a test print
    film: Film
    print_text: PrintText | None # None in a test print
    adjustment: MachineFilmAdjustment | None
    lot: str = ""
    countries: list[str] = []
    free_text: dict[str, str] = {}
    origin_line: str = ""        # bag text: "Product of USA and Canada", "USA (pre-printed)" or ""
    bag_text: str = ""           # one-line rendering of what will print today, for windows and logs
    settings: PrintSettings
    position: PositionWorking
    warnings: list[Msg] = []
    test_step: TestStep | None = None   # set only in a test print or calibration

@dataclass(kw_only=True)
class SendPlan:
    send_id: str
    request: SendRequest
    machines: list[MachinePlan]
    from_local_copy: bool = False
    data_as_of: float | None = None     # HxData.reached_at, shown as "templates as of <time>"
    def restricted(self, machine_ids: list[str]) -> "SendPlan": ...
        # Try again: the same choices for these machines only, under a new send_id; both `machines`
        # and `request.lines[i].machine_ids` are filtered, empty lines dropped (send.md §4.5)

@dataclass(kw_only=True)
class TestRanges:
    vary: str                    # "position" (encoder machines) or "delay" (delay machines)
    start: float                 # mm or ms
    stop: float
    step: float
    bold: list[int] = []         # extra values to try; [] keeps the film's
    width: list[int] = []
    resolution: list[int] = []
    fonts: list[tuple[int, int]] = []   # printed side by side in each message; [] = the film's font and size

@dataclass(kw_only=True)
class TestPlan:
    run_id: str                  # new_id("tp")
    kind: str                    # "test" | "spacing" | "head_distance"
    base: MachinePlan            # built from the machine and film with adjustments ignored
    steps: list[TestStep]
    bags_per_step: int = 5
    timed: bool = False          # True when Company.bag_counter == "none"
```

### 5.7 Commands and read-back

```python
@dataclass(frozen=True)
class Command:
    tag: str                     # outer tag: "CRTMSG", "PRIN", ...
    xml: str                     # the whole APCMD content: "<PRINT>1</PRINT>" or "<PRIN/>"
    write: bool                  # True for setting and design commands (answer must be "0")
    @staticmethod
    def query(tag: str) -> "Command": ...                 # Command(tag, f"<{tag}/>", False)
    @staticmethod
    def set(tag: str, value: str | int) -> "Command": ...  # Command(tag, f"<{tag}>{value}</{tag}>", True)
    @staticmethod
    def block(tag: str, children: list[tuple[str, str | int]]) -> "Command": ...
        # Command(tag, "<TAG><A>1</A><B>x</B></TAG>", True); text values are XML-escaped (protocol.md)

@dataclass(kw_only=True)
class DesignItem:
    tag: str                     # "CTEXT", "CTDAT", "CLOT", ... as in the FCDOC
    pairs: list[tuple[str, str]] # every child tag and its text, in reply order (tags can repeat, e.g. LANG)
    def value(self, tag: str, default: str | None = None) -> str | None: ...   # the last occurrence

@dataclass(kw_only=True)
class MessageDesign:
    """The printer's own copy of one stored message, from the web Load."""
    slot: int                    # MSNO of the reply: the real slot, never 0
    name: str                    # MSGNAM ("" when unnamed)
    empty: bool                  # the reply had no FCDOC
    items: list[DesignItem] = []
    settings: dict[str, str] = {}  # the message's CPPARM: child tag -> text
    width: int | None = None     # W
    height: int | None = None    # H
    black_pixels: int = 0        # BPIX
    preview_bmp: bytes = b""     # the decoded 1-bit 1024x160 BMP
    read_at: float = 0.0
    tn: str = ""                 # the request numbers this reply matched
    xid: int = 0

@dataclass(kw_only=True)
class BuiltMessage:
    """What build_commands sends, and what the read-back must show."""
    slot: int
    name: str
    content_code: str
    commands: list[Command]      # CRTMSG, one per item, CPPARM, SAVEMSG <slot>
    expected_items: list[DesignItem]   # as sent (compare applies the read-back rules)
    expected_settings: dict[str, str]  # every CPPARM value the read-back must show, incl. printer defaults
    origin_line: str
    bag_text: str

@dataclass(frozen=True)
class Difference:
    where: str                   # "item 2 (CTEXT)", "print settings", "name", "slot"
    field: str                   # "TXT", "SIZE", "PRNSPD", ...
    sent: str
    read: str

@dataclass(frozen=True)
class MessageName:
    film_code: str
    print_text_code: str
    print_text_revision: int
    content_code: str
    slot: int
```

### 5.8 Status files

The dataclasses below are the contents of the status files. storage.md owns the file names, the
envelope and how they are written; states.md owns what the poller puts in `PollStatus`,
`MachineSnapshot` and `Alert`; send.md owns what senders put in `ExpectedState` and `LineStatus`.
The optional fields those two files added (states.md §11.2, send.md §1.4) are written into the blocks
below, marked with their owner, so the `hx_model` builder takes every field from this section alone.
`from_json` ignores unknown keys, so a field missing here would be silently dropped on every read: a
spec file that adds a field must add it here too.

```python
@dataclass(kw_only=True)
class LockInfo:                  # status\locks\<printer_id>.lock
    printer_id: str
    token: str                   # random hex, new for every acquisition
    user: str
    computer: str
    pid: int
    purpose: str                 # LOCK_PURPOSES
    line_id: str = ""
    machine_id: str = ""
    run_id: str = ""             # send_id, test run_id or action id
    counter: int = 0             # +1 at every refresh; staleness is judged from this only
    acquired_at: float = 0.0     # display only
    note: dict = {}              # e.g. {"step": 12, "of": 40} during a test print

@dataclass(kw_only=True)
class HeartbeatInfo:             # status\pollers\<key>.json
    key: str                     # Identity.key
    computer: str
    user: str
    pid: int
    version: str
    counter: int = 0             # +1 every heartbeat_s
    written_at: float = 0.0
    can_poll: bool = False       # reached any printer within printer_reach_window_s
    last_printer_reach: float | None = None
    polling: list[str] = []      # printer ids this PC is polling now
    monitor_open: bool = False
    floor_board: bool = False
    declined: list[str] = []     # printer ids this PC won't poll for now: a stuck worker (states.md §6.2 item 3.0)

@dataclass(kw_only=True)
class RunningMessage:
    slot: int
    name: str
    fingerprint: str             # hx_message.fingerprint of the read-back
    origin_line: str             # bag text read back ("" if the message has no origin item)
    read_at: float
    preview_sha256: str = ""     # of status\machines\<id>.preview.bmp

@dataclass(kw_only=True)
class MachineSnapshot:           # the poller's judgement (states.md defines the codes)
    code: str
    colour: str                  # COLOURS
    headline: Msg
    since: float
    detail: list[Msg] = []

@dataclass(kw_only=True)
class Alert:
    id: str                      # stable while the alert lasts; also the ack file name
    machine_id: str
    line_id: str
    kind: str                    # states.md list
    sound: bool                  # False: warning without sound
    msg: Msg
    started_at: float
    recipients: list[str] = []   # "user:<DOMAIN\\name>", "floor", "admins", "monitor"
    active: bool = True
    # added by states.md §11.2:
    colour: str = ""             # RED, AMBER or "" (doesn't colour the machine)
    ref: str = ""                # the send_id or run_id the alert belongs to; "" for time-based ids
    acked_by: str = ""
    acked_at: float | None = None
    ended_at: float | None = None
    waiting: list[str] = []      # recipients with no live heartbeat yet (states.md §4.6)

@dataclass(kw_only=True)
class Ack:                       # status\alerts\<alert_id>.json, written once
    alert_id: str
    by: str
    computer: str
    at: float

@dataclass(kw_only=True)
class PollStatus:                # status\machines\<machine_id>.poll.json; one writer: the assigned poller
    machine_id: str
    printer_id: str
    poller: str                  # writer's Identity.key
    counter: int = 0             # +1 every cycle
    written_at: float = 0.0
    paused_for_lock: str = ""    # LockInfo.token it stepped aside for; "" while polling
    yielded_to: str = ""         # heartbeat key it handed the printer to (§7.3)
    reachable: bool = False
    failures: int = 0            # failed requests in a row
    last_answer_at: float | None = None
    printing: bool | None = None
    message_name: str | None = None
    bag_count: int | None = None         # the Company.bag_counter value
    bags_recent: int | None = None       # prints within silent_window_s
    ink_name: str | None = None
    ink_level: int | None = None
    head_temp_c: int | None = None
    clock_offset_s: float | None = None  # printer clock minus time-server time
    running: RunningMessage | None = None
    daily: dict = {}             # last daily check (states.md)
    state: MachineSnapshot | None = None
    alerts: list[Alert] = []
    foreign_client: bool = False # "Something else is talking to printer X"
    # added by states.md §11.2:
    problem: str = ""            # "" | "wrong_printer" | "login_failed"
    expected_counter: int = 0    # ExpectedState.counter at the last judgement (0: there was no E)
    reread_seen: str = ""        # the last ExpectedState.reread_token acted on
    unreachable_since: float | None = None   # wall time reachable last became False; None while reachable
    running_ok_at: float | None = None       # wall time of the last successful running-message Load (display)
    running_unchecked: bool = False          # committed fact: the running message can't be read (states.md §6.5)
    dst: dict = {}               # the daylight-saving clock set of the current change (states.md §6.11)
    standalone: bool = False     # True only in Poller.local_statuses() in standalone mode
    judged: int | None = None    # +1 only by a write that judged a reading, or a paused write; None: older file
    judged_at: float = 0.0       # wall time of that write (display and first sight only; states.md §6.14)
    printing_unchecked: bool = False   # committed fact: PRIN unread for PRIN_UNREAD_LIMIT cycles (states.md §6.6)

@dataclass(kw_only=True)
class FailureInfo:
    step: str                    # STEP_*
    msg: Msg
    after_switch: bool
    at: float

@dataclass(kw_only=True)
class ExpectedState:             # status\machines\<machine_id>.expected.json; written under the printer lock
    machine_id: str
    counter: int = 0
    written_at: float = 0.0
    written_by: str = ""
    computer: str = ""
    mode: str = "none"           # "none" | "switch_pending" | "running" | "stopped_by_user" |
                                 # "stop_requested_at_printer" | "test_running" | "test_finished"
    printing_expected: bool = False
    send_id: str = ""
    message_name: str = ""
    content_code: str = ""
    fingerprint: str = ""
    slot: int | None = None
    origin_line: str = ""        # bag text
    previous_message_name: str = ""   # while switch_pending: what was printing before
    previous_fingerprint: str = ""
    result: str = ""             # the last Send's colour for this machine: GREEN, AMBER or RED
    failure: FailureInfo | None = None
    notes: list[str] = []        # amber reasons: "pc_time", "no_lock", "local_copy"
    confirmed: bool = False
    confirmed_by: str = ""
    confirmed_at: float | None = None
    rejected: bool = False
    resume_blocked: bool = False
    left_by: str = ""            # "Leave it"
    left_at: float | None = None
    reread_token: str = ""       # a new value asks the poller to load the running message now
    test: dict | None = None     # {"run_id": ..., "kind": ..., "by": ..., "step": 12, "of": 40}
    # added by send.md §1.4:
    sender: str = ""             # who made the Send (or test) this state belongs to; written_by is only
                                 # the last writer (a confirm, Leave it or clock set doesn't change it)
    stop_reason: str = ""        # "user" | "rejected" | "test"; "" while not stopped
    stopped_by: str = ""
    stopped_at: float | None = None

@dataclass(kw_only=True)
class LineStatus:                # status\lines\<line_id>.json; informational: the line's last Send
    line_id: str
    counter: int = 0
    written_at: float = 0.0
    written_by: str = ""
    send_id: str = ""
    sent_at: float | None = None
    product_id: str = ""
    film_id: str = ""
    film_revision: int = 0
    print_text_id: str = ""
    print_text_revision: int = 0
    lot: str = ""
    countries: list[str] = []
    origin_line: str = ""
    machines: dict[str, str] = {}   # machine_id -> that machine's result colour
    confirmed: bool = False
    confirmed_by: str = ""
    stopped_needs_send: bool = False
    # added by send.md §1.4:
    sender: str = ""             # who made the Send this file describes
```

### 5.9 Results and events

```python
@dataclass(kw_only=True)
class MachineResult:
    machine_id: str
    colour: str                  # AMBER (live, checked, not yet confirmed by the sender) or RED
    ok: bool                     # went live and both running-message reads passed
    step: str                    # the last step reached; the failed step when not ok
    msg: Msg | None = None
    after_switch: bool = False
    slot: int | None = None
    message_name: str = ""
    content_code: str = ""
    fingerprint: str = ""
    origin_line: str = ""
    readbacks: list[MessageDesign] = []   # idle slot, running 1, running 2, as far as reached
    previous: RunningMessage | None = None   # what it was printing just before this Send
    previous_preview: bytes = b""
    previous_confirmed: bool = False         # previous is the last Send's confirmed message (send.md §10.5)
    counts_before: int | None = None
    counts_after: int | None = None
    notes: list[str] = []        # "pc_time", "no_lock", "local_copy", "ink_mismatch", "film_stop"

@dataclass(kw_only=True)
class SendResult:
    send_id: str
    sender: str
    computer: str
    started_at: float
    finished_at: float
    machines: list[MachineResult]
    time_source: str             # "server" | "pc"
    from_local_copy: bool = False
    data_as_of: float | None = None
    log_name: str = ""           # file name in logs\hx-sends, or in the PC's queue
    log_queued: bool = False

@dataclass(kw_only=True)
class ActionResult:              # Stop, Resume, Set clocks
    action_id: str
    kind: str                    # "stop" | "resume" | "clock"
    machines: list[MachineResult]

@dataclass(kw_only=True)
class ResumeCheck:
    machine_id: str
    design: MessageDesign | None
    origin_line: str
    allowed: bool
    msg: Msg | None = None       # why not

@dataclass(kw_only=True)
class TestResult:
    run_id: str
    kind: str
    steps_done: int
    of: int
    ok: bool
    msg: Msg | None = None       # why it stopped early
    cleared: bool = False        # slots 9 and 10 were overwritten with empty messages
    printing_off: bool = False   # the end procedure read printing off twice (send.md §15.5)

# Events: posted on the EventBus. All plain data; never Tk objects.
@dataclass(kw_only=True)
class SendEvent:
    send_id: str
    machine_id: str = ""         # "" for whole-send events
    step: str                    # STEP_*
    status: str                  # "running" | "ok" | "failed" | "skipped" | "warning"
    msg: Msg | None = None
    design: MessageDesign | None = None
    at: float = 0.0

@dataclass(kw_only=True)
class SendFinished:
    result: SendResult

@dataclass(kw_only=True)
class ActionEvent:
    action_id: str
    kind: str                    # "stop" | "resume" | "clock"
    machine_id: str
    status: str                  # as SendEvent
    msg: Msg | None = None
    design: MessageDesign | None = None
    at: float = 0.0

@dataclass(kw_only=True)
class ActionFinished:
    result: ActionResult

@dataclass(kw_only=True)
class TestEvent:
    run_id: str
    step_index: int
    of: int
    status: str                  # "building" | "printing" | "ok" | "failed" | "stopping" | "cleared"
    msg: Msg | None = None
    design: MessageDesign | None = None
    bags: int | None = None
    at: float = 0.0

@dataclass(kw_only=True)
class TestFinished:
    result: TestResult

@dataclass(kw_only=True)
class BoardUpdate:
    board: "BoardView"

@dataclass(kw_only=True)
class DataUpdate:
    data: HxData

@dataclass(kw_only=True)
class ShareHealth:
    ok: bool
    last_reached: float | None

@dataclass(kw_only=True)
class PollerUpdate:
    mode: str                    # "share" | "standalone" | "idle"
    polling: list[str] = []      # printer ids
    next_check_s: float | None = None   # standalone: seconds to the next check
```

### 5.10 Views

Computed by `hx_state` from status files; shown by the UI as they are. The UI never decides a colour,
a state or whether a button is allowed; it reads these fields.

```python
@dataclass(kw_only=True)
class MachineView:
    machine_id: str
    line_id: str
    name: str                    # the record's name, not translated
    colour: str                  # COLOURS
    code: str                    # state code (states.md)
    headline: Msg
    details: list[Msg] = []
    origin_line: str = ""        # bag text: what it is confirmed (or last read) printing
    message_name: str = ""
    checked_age_s: float | None = None   # "checked N s ago"; None when never read
    monitored: bool = False
    printing: bool | None = None
    alerts: list[Alert] = []     # active ones, with acks applied
    unacked_sound: bool = False
    can_stop: bool = False
    can_resume: bool = False
    needs_send: bool = False
    confirm_pending_by: str = "" # sender who still has to confirm ("Send not confirmed")
    confirm_send_id: str = ""
    test: dict | None = None     # {"by": ..., "step": 12, "of": 40}
    has_preview: bool = False

@dataclass(kw_only=True)
class LineView:
    line_id: str
    name: str
    colour: str                  # its worst machine
    headline: Msg
    machines: list[MachineView]
    origins_differ: bool = False
    monitored: bool = False
    checked_age_s: float | None = None

@dataclass(kw_only=True)
class BoardView:
    lines: list[LineView]
    share_ok: bool
    standalone: bool = False     # share down: this PC's own checks
    next_check_s: float | None = None
    generated_at: float = 0.0

@dataclass(kw_only=True)
class FloorRow:                  # one big row per machine
    line_id: str
    machine_id: str
    colour: str
    line_name: str
    machine_name: str
    origin_line: str             # bag text
    status: Msg
    checked_age_s: float | None
```

## 6. Module interfaces

Signatures are fixed. "Owner" specifies behaviour. Threading notes say which thread may call what.

### 6.1 hx_message (owner: send.md). Pure.

```python
ORIGIN_WORDS: tuple[str, ...]   # "Product of", "Grown in", "Origin", "Made in", "Hecho en"
DATE_FORMATS: dict[str, str]    # builder choice -> printer FRMT codes, e.g. "MM/DD/YY" -> "M/D/y"

def origin_phrase(countries: list[str]) -> str
    # "Product of USA", "Product of USA and Canada", "Product of USA, Canada, and Mexico"
def origin_words_in(text: str, company: Company) -> list[str]
    # origin words, country names or aliases found, ignoring case and punctuation; [] when clean
def validate_print_text(pt: PrintText, company: Company) -> list[Msg]
def validate_film(film: Film, data: HxData) -> list[Msg]
def validate_machine(machine: Machine, data: HxData) -> list[Msg]
def films_for_line(line: Line, product_id: str, data: HxData) -> list[Film]     # active, same cartridge
def print_texts_for_film(film: Film, data: HxData) -> list[PrintText]           # active, allowed, origin rule
def validate_choice(choice: LineChoice, data: HxData, identity: Identity) -> list[Msg]
def merge_plan(request: SendRequest, data: HxData, identity: Identity) -> SendPlan   # raises PlanError

def print_position_mm(position_mm: float, head_distance_mm: float, film_length_mm: float,
                      adjustment_mm: float = 0.0) -> int
    # (position - head distance + adjustment) mod film length, rounded to whole mm, in [0, film length)
def delay_us(delay_ms: float, adjustment_ms: float = 0.0) -> int
    # round((delay + adjustment) * 1000); raises PlanError below 0 or above DELAY_US_MAX
def explain_position(machine: Machine, film: Film, adjustment: MachineFilmAdjustment | None,
                     *, override: float | None = None, test: bool = False) -> PositionWorking
def film_stop_warning(sent_mm: int, print_length_mm: float | None, film_length_mm: float) -> Msg | None

def content_code(plan: MachinePlan) -> str
def message_name(plan: MachinePlan, slot: int) -> str           # at most MESSAGE_NAME_MAX characters
def parse_message_name(name: str) -> MessageName | None         # None unless the check letter is right
def idle_slot(printing: bool, printing_slot: int | None) -> int # the slot of SLOTS not printing; 9 by default

def build_message(plan: MachinePlan, slot: int) -> BuiltMessage
def build_commands(plan: MachinePlan, slot: int) -> list[Command]   # == build_message(plan, slot).commands
def empty_message_commands(slot: int) -> list[Command]          # end of a test: an empty message saved to slot
def clock_command(local: datetime) -> Command                   # TD: date, time and midnight rollover

def compare(sent: BuiltMessage, readback: MessageDesign) -> list[Difference]   # [] when it matches
def fingerprint(design: MessageDesign) -> str                   # sha256 hex of the normalised design
def origin_from_design(design: MessageDesign) -> str            # the origin line as read back, or ""

def plan_test(machine_id: str, film_id: str, ranges: TestRanges, data: HxData, identity: Identity,
              *, kind: str = "test") -> TestPlan                # raises PlanError
def test_step_plan(base: MachinePlan, step: TestStep) -> MachinePlan
def head_distance_from_print(sent_mm: int, measured_mm: float, film_length_mm: float,
                             rough_mm: float) -> float          # OPEN: see §11
```

### 6.2 hx_state (owner: states.md). Pure.

```python
@dataclass(kw_only=True)
class Reading:                   # one poll cycle's facts; states.md may add optional fields
    at: float                    # poller's clock.time()
    reachable: bool
    printing: bool | None = None
    message_name: str | None = None
    bag_count: int | None = None
    ink_name: str | None = None
    ink_level: int | None = None
    head_temp_c: int | None = None
    clock_offset_s: float | None = None
    running: RunningMessage | None = None   # set on cycles that loaded the running message
    nonsense: int = 0            # replies thrown away this cycle
    second: bool = False         # this is the confirming second reading

@dataclass(kw_only=True)
class Judgement:
    state: MachineSnapshot
    alerts: list[Alert]          # the machine's full active alert list after this reading
    recheck: list[str] = []      # items to read again after second_reading_s before acting
    read_running_now: bool = False

STATE_CODES: dict[str, str]      # code -> colour; the full table is in states.md

def judge(prev: PollStatus | None, reading: Reading, expected: ExpectedState | None,
          lock: LockInfo | None, machine: Machine, line: Line, data: HxData,
          timings: Timings = TIMINGS) -> Judgement
def machine_view(machine: Machine, poll: PollStatus | None, poll_age_s: float | None,
                 expected: ExpectedState | None, lock: LockInfo | None, lock_age_s: float | None,
                 acks: dict[str, Ack], timings: Timings = TIMINGS) -> MachineView
def line_view(line: Line, machines: list[MachineView], status: LineStatus | None) -> LineView
def board_view(data: HxData, polls: dict[str, PollStatus], poll_ages: dict[str, float],
               expected: dict[str, ExpectedState], locks: dict[str, LockInfo],
               lock_ages: dict[str, float], lines: dict[str, LineStatus], acks: dict[str, Ack],
               *, share_ok: bool, standalone: bool, now: float,
               timings: Timings = TIMINGS) -> BoardView
def worst(colours: list[str]) -> str
def floor_board_rows(board: BoardView) -> list[FloorRow]
def sounding_alerts(board: BoardView, identity: Identity, *, floor_board: bool) -> list[Alert]
    # unacknowledged sound alerts this PC should be ringing for
```

### 6.3 hx_win (owner: this file, complete)

Every function works on any platform: on Windows through ctypes (and `winsound`), elsewhere through
the fallback shown. Off Windows, and in an unfrozen run on Windows, the environment can set the
identity (for development and tests); a frozen exe ignores those variables.

```python
def is_windows() -> bool

def current_identity() -> Identity
    # Windows: secur32.GetUserNameExW(NameSamCompatible=2) -> "EXAMPLE\\jsmith";
    #   GetComputerNameW; OpenProcessToken + GetTokenInformation(TokenGroups) +
    #   ConvertSidToStringSidW for every group with SE_GROUP_ENABLED; os.getpid().
    # Overrides (unfrozen only): STRIPES_HX_USER, STRIPES_HX_COMPUTER, STRIPES_HX_SIDS (comma-separated).
    # Off Windows without overrides: user "LOCAL\\" + getpass.getuser(), socket.gethostname(), no SIDs.

def lookup_group_sid(name: str) -> str | None
    # LookupAccountNameW + ConvertSidToStringSidW; None if not found or off Windows

def open_for_read(path: str) -> BinaryIO
    # Windows: CreateFileW(GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
    #   OPEN_EXISTING) -> msvcrt.open_osfhandle -> os.fdopen(fd, "rb"). Windows' os.replace (MoveFileExW)
    #   can still refuse to replace a file a reader holds open, so writers retry (storage §8.2 item 3).
    #   Raises FileNotFoundError / PermissionError / OSError like open().
    # Elsewhere: open(path, "rb").

@dataclass(frozen=True)
class Monitor:
    index: int; x: int; y: int; width: int; height: int; primary: bool

def monitors() -> list[Monitor]
    # Windows: EnumDisplayMonitors + GetMonitorInfoW, primary first; elsewhere [] (the UI then uses Tk's screen)

def play_alert(repeat: bool = True) -> None
    # winsound.PlaySound(<bundled assets/alert.wav>, SND_FILENAME | SND_ASYNC | (SND_LOOP if repeat));
    # elsewhere a no-op. Idempotent while already playing.
def stop_alert() -> None         # winsound.PlaySound(None, 0); elsewhere a no-op

def self_check() -> None
    # calls current_identity, lookup_group_sid("Administrators"), open_for_read on a temp file,
    # monitors and PlaySound(None, 0); raises on any failure (used by the exe's --self-check)
```

The bundled file is found the way `stripes.resource_path` does: `getattr(sys, "_MEIPASS",
<this file's folder>)` joined with `assets/alert.wav`. `hx_win` must not import `stripes`.

### 6.4 hx_protocol (owner: protocol.md)

```python
class GateRegistry:
    """One threading.Lock per printer, so a process never has two requests in flight to it."""
    def gate(self, printer_key: str) -> threading.Lock: ...    # printer_key = f"{host}:{port}"
PROCESS_GATES = GateRegistry()   # the app uses this one; a simulated PC in a test gets its own

def make_token(password: str) -> str            # base64 of the ASCII md5 hex of the password
def password_md5(token: str) -> str             # the md5 hex back from a token, for the web login
def scramble(token: str, key: bytes) -> str     # the stored form (Printer.token)
def unscramble(stored: str, key: bytes) -> str  # raises AuthFailed for a damaged value or another key
def release_key() -> bytes                      # hx_key.KEY when present, else a fixed development key

@dataclass(frozen=True)
class ReplySpec:
    kind: str                    # "int" | "str" | "date" | "time"
    min: int | None = None
    max: int | None = None
    allowed: tuple | None = None # e.g. (0, 1) for PRIN
    none_ok: bool = False        # "None" (empty) is a valid answer
QUERY_SPECS: dict[str, ReplySpec]   # every query Stripes may send, and how its reply is checked

@dataclass(kw_only=True)
class SmeReply:
    type: str                    # "Load", "Save", "Thumbnail", ...
    tn: str
    xid: str
    msno: int | None
    design: MessageDesign | None
def parse_sme_reply(body: bytes) -> SmeReply     # raises WebReadFailed for an unrecognised body

class PrinterClient:
    """Talks to one printer. Thread-safe: every request, and the whole of load_slot, holds the
    printer's gate from the registry, so callers on different threads take turns."""
    def __init__(self, host: str, uid: int, token: str, *, port: int = 80, clock: Clock = SYSTEM_CLOCK,
                 gates: GateRegistry = PROCESS_GATES, timings: Timings = TIMINGS) -> None: ...
        # token is the plain token (make_token); ClientPool unscrambles Printer.token before this
    host: str; port: int; uid: int
    nonsense: int                # replies thrown away so far (BadReply, mixed-up web replies)
    last_request_at: float | None    # clock.monotonic() of the last request
    def query(self, tag: str, *, deadline: float | None = None) -> str: ...
        # one Autopilot query; the reply text, stripped. Re-asked once after read_retry_s on
        # NoReply or PrinterBusy. Raises ForbiddenCommand for FORBIDDEN_QUERY_TAGS.
        # deadline (clock.monotonic()): the gate wait is bounded by it, and a gate acquired later than
        # deadline - worst_case_s(call) is released at once with PrinterBusy, nothing sent (protocol.md §9)
    def read(self, tag: str, *, deadline: float | None = None) -> int | str | None: ...
        # query + QUERY_SPECS check; a failed check is thrown away and asked once more; then BadReply;
        # deadline as query
    def read_twice(self, tag: str, *, deadline: float | None = None) -> int | str | None: ...
        # two reads second_reading_s apart that must agree (BadReply otherwise); deadline as query, for each read
    def command(self, cmd: Command, *, deadline: float | None = None) -> None: ...
        # deadline as query; one write, never repeated.
        # Raises ForbiddenCommand unless cmd.tag in ALLOWED_WRITE_TAGS.
        # After idle_warmup_s of quiet, sends a warm-up read (HXUI, checked) first. "0" -> returns;
        # another code -> CommandRefused; no reply -> NoReply (the write may or may not have happened)
    def check_uid(self, *, deadline: float | None = None) -> None: ...   # raises WrongPrinter; deadline as read_twice
    def login(self, *, deadline: float | None = None) -> None: ...
        # web login; raises AuthFailed. deadline as query: the gate wait never passes it (PrinterBusy)
    def load_slot(self, slot: int, *, deadline: float | None = None) -> MessageDesign: ...
        # slot 0..10 (0 = printing now). Logs in when needed, Load then sme_repeat until the reply
        # carries this request's TN and XID; raises WebReadFailed after web_repeat_max or web_repeat_window_s.
        # deadline (clock.monotonic()): no exchange starts that couldn't end by it (protocol.md §10.6)
    def fonts(self, *, deadline: float | None = None) -> list[str]: ...
        # GET /sfl.xml (protocol.md §10.8), with load_slot's deadline rule; OPEN: the source
    def firmware(self, *, deadline: float | None = None) -> str | None: ...   # GET /idn.xml; as fonts
    def probe(self) -> bool | None: ...             # bare TCP connect under the gate, nothing sent;
                                                    # None when the gate was busy (protocol.md §9)

class ClientPool:
    """One PrinterClient per printer record, rebuilt when host, port, uid or token change."""
    def __init__(self, *, key: bytes, clock: Clock = SYSTEM_CLOCK, gates: GateRegistry = PROCESS_GATES,
                 timings: Timings = TIMINGS) -> None: ...
    def client(self, printer: Printer) -> PrinterClient: ...    # raises AuthFailed if the token is unusable
```

Transport rules fixed here: plain HTTP to port 80, one request per TCP connection, never through a
proxy (never `urllib.request`; `http.client.HTTPConnection` or a raw socket only). The client never
requests the web editor page (`GET /simple_msg.html`). The only web requests are the login POST,
`POST /simple_msg.html?sme` (Load), `POST /simple_msg.html?sme_repeat`, and the two small files of
`hx_protocol.WEB_FILES`: `GET /sfl.xml` (the font list) and `GET /idn.xml` (the firmware version),
which `fonts()` and `firmware()` need because no Autopilot query returns either. Those two GETs are
made only under a held printer lock (a Send's or test's machine check) or by the assigned poller after
its lock check (the daily check). This widens the plan's "the only web request Stripes makes" and is
listed for the owner's sign-off (§12.1). OPEN: confirm on the test unit that the files need the login
cookies and that fetching them doesn't disturb printing.

### 6.5 hx_time (owner: send.md)

```python
@dataclass(frozen=True, kw_only=True)
class TimeReading:
    utc: float                   # epoch seconds
    local: datetime              # naive local time, by this PC's time-zone rules
    source: str                  # "server" | "pc"
    server: str = ""             # the host that answered
    offset_s: float = 0.0        # server minus this PC's clock; 0.0 for "pc"

def sntp_query(host: str, *, port: int = 123, timeout_s: float = 2.0) -> float
    # one SNTP request over UDP; returns the server's UTC epoch seconds; raises TimeServerError
def local_utc_offset(utc: float) -> float     # seconds east of UTC at that moment (time.localtime)

class TimeSource:
    def __init__(self, hosts: list[str], *, clock: Clock = SYSTEM_CLOCK, query=sntp_query,
                 offset_fn=local_utc_offset, cache_s: float = 600.0) -> None: ...
    def set_hosts(self, hosts: list[str]) -> None: ...   # from Company.time_server and _fallback
    def now(self, *, fresh: bool = False) -> TimeReading: ...
        # never raises: fresh=True asks the server(s) now; otherwise a cached offset younger than
        # cache_s is used, and a failed query is remembered for cache_s (no new query meanwhile).
        # No answer -> source "pc" (the caller warns and marks the result amber)
    def cached(self) -> TimeReading: ...
        # never queries and never blocks: the cached server offset (send.md §9.2), else the PC's time
    offset_fn: Callable[[float], float]   # the offset function given; the poller's DST rules use it

def recent_dst_change(utc: float, *, within_s: float = 86_400.0,
                      offset_fn=local_utc_offset) -> tuple[float, float, float] | None
    # (old offset, new offset, the UTC second of the change) when the offset changed within within_s
    # (send.md §9.3 item 5); the poller's daylight-saving clock set uses it (states.md §6.11). There is
    # no DstWatcher class: a change is found from the time alone, so a PC started after it still sees it
```

Thread notes: `TimeSource` is thread-safe (a lock around its cache); `now()` can block for up to
`timeout_s` per host (plus name resolution, which has no time limit), so it's called from worker
threads only, and never from a poller worker: the poller uses `cached()`, and its `hx-time` thread
keeps the cache fresh (§7.1).

### 6.6 hx_share (owner: storage.md)

```python
@dataclass(frozen=True, kw_only=True)
class HxPaths:
    share_root: str | None       # the v2 folder on the share, or None
    roaming: str                 # %APPDATA%\Stripes\v2
    local: str                   # %LOCALAPPDATA%\Stripes\v2
    machine: str                 # %PROGRAMDATA%\Stripes\v2 (shared by the PC's users)

def v2_share_root(config_path: str | None) -> str | None   # §10.5
def roaming_dir() -> str
def local_dir() -> str
def machine_dir() -> str
def paths(config_path: str | None) -> HxPaths
def copy_once(old_path: str, new_path: str) -> bool        # copies a file if new is missing and old exists

class FileOps:
    """The raw file operations ShareStore uses; replaced in tests to fake a slow or dead share."""
    def read_bytes(self, path: str) -> bytes: ...           # via hx_win.open_for_read
    def write_whole(self, path: str, data: bytes) -> None: ...   # temp file + os.replace (with retries)
    def create_exclusive(self, path: str, data: bytes) -> bool: ...
        # O_CREAT|O_EXCL, then the data through the same handle; False if it exists.
        # Also how add-only logs are written (once, under their final name)
    def rename(self, src: str, dst: str) -> bool: ...       # False if src is gone or dst exists
    def listdir(self, path: str) -> list[str]: ...
    def makedirs(self, path: str) -> None: ...
    def remove(self, path: str) -> None: ...
    def exists(self, path: str) -> bool: ...
class FlakyFileOps(FileOps):     # test helper: down, slow_s, hide_new_s (directory-cache delay)
    down: bool; slow_s: float; hide_new_s: float

class CounterWatch:
    """Judges freshness by change, on this PC's own monotonic clock."""
    def __init__(self, clock: Clock) -> None: ...
    def see(self, key: str, counter: int) -> float: ...     # seconds since this counter last changed (0.0 on change or first sight)
    def age(self, key: str) -> float | None: ...

class ShareStore:
    """Every share access. Each operation runs on an 'hx-io' worker thread (a small pool) and is
    abandoned after share_timeout_s (ShareUnavailable); a stuck worker is replaced. Then the share
    counts as down and calls fail at once until a background probe succeeds (every share_retry_s).
    Thread-safe."""
    def __init__(self, root: str | None, *, identity: Identity, clock: Clock = SYSTEM_CLOCK,
                 fs: FileOps | None = None, timings: Timings = TIMINGS, version: str = "dev") -> None: ...
    root: str | None
    available: bool
    last_reached: float | None   # clock.time() of the last successful operation
    watch: CounterWatch          # shared by everything reading status files through this store
    local_locks: dict[str, str]  # printer_id -> run_id of in-process locks (§7.2)
    def local_lock_info(self, printer_id: str) -> LockInfo | None: ...
        # a copy of the LockInfo registered with local_locks (with the latest update() note), or None;
        # no share access (storage.md §11.2 step 2); the standalone status watcher shows it as blue
    # generic
    def read_json(self, rel: str) -> dict | None: ...       # None if missing; FormatTooNew; ShareUnavailable
    def write_json(self, rel: str, data: dict) -> None: ...
    def append_log(self, kind: str, entry: dict) -> str: ...   # LOG_KINDS; add-only; returns the file name
    def read_logs(self, kind: str, *, since: float | None = None) -> list[dict]: ...  # complete entries only
    # status
    def read_poll(self, machine_id: str) -> PollStatus | None: ...
    def write_poll(self, status: PollStatus) -> None: ...
    def read_expected(self, machine_id: str) -> ExpectedState | None: ...
    def write_expected(self, state: ExpectedState) -> None: ...
    def read_line_status(self, line_id: str) -> LineStatus | None: ...
    def write_line_status(self, status: LineStatus) -> None: ...
    def read_ack(self, alert_id: str) -> Ack | None: ...    # opened by name, never by listing
    def write_ack(self, ack: Ack) -> None: ...
    def read_preview(self, machine_id: str) -> bytes | None: ...
    def write_preview(self, machine_id: str, bmp: bytes) -> None: ...
    # locks and heartbeats
    def read_lock(self, printer_id: str) -> LockInfo | None: ...   # opened by name; updates self.watch
    def lock(self, printer_id: str, *, purpose: str, run_id: str, line_id: str = "",
             machine_id: str = "") -> "PrinterLock": ...
    def heartbeat(self) -> "Heartbeat": ...
    def live_heartbeats(self) -> list[HeartbeatInfo]: ...   # counter moved within stale_s, same FORMAT

class PrinterLock:
    """Thread-safe: acquire() and start_keeping() on one thread; check(), update() and release() from
    any thread (a Send takes the locks on its whole-Send thread and its machine threads use them)."""
    printer_id: str
    info: LockInfo | None
    state: str                   # "unheld" | "held" | "local_only" | "lost"
    def acquire(self) -> str: ...
        # registers in store.local_locks first; then creates the lock file. Returns "held", or
        # "local_only" when the share is unreachable (the caller marks the result amber).
        # A lock whose counter this store has seen unchanged for lock_stale_s is taken over
        # (renamed aside first, so only one PC wins). Raises LockHeld(info) for a live holder.
    def start_keeping(self) -> None: ...   # 'hx-lock-<printer>' thread bumps the counter every lock_refresh_s
    def update(self, **note) -> None: ...  # e.g. update(step=12, of=40); written at the next refresh
    def check(self, budget_s: float = 0.0) -> None: ...
        # before every printer request: raises LockLost when the lock is lost, or when the next call
        # (lasting up to budget_s) could still run when another PC may judge the lock stale (storage.md §11.3)
    def lease_end(self) -> float | None: ...
        # last_ok + lock_stale_s / 2, the deadline of every printer call under this lock; None when
        # local_only or unheld; raises LockLost when lost (storage.md §11.3)
    def release(self) -> None: ...
        # idempotent; stops the keeper, removes the file while the lease still runs, and the local entry
    def __enter__(self) -> "PrinterLock": ...
    def __exit__(self, *exc) -> None: ...

class Heartbeat:
    key: str
    def beat(self, *, can_poll: bool, last_printer_reach: float | None, polling: list[str],
             monitor_open: bool, floor_board: bool, declined: tuple[str, ...] = ()) -> None: ...
    def remove(self) -> None: ...          # on a clean exit

def assign(printer_ids: list[str], heartbeats: list[HeartbeatInfo]) -> dict[str, list[str]]
    # pure: printer_id -> heartbeat keys of pollers able to poll, in rank order (rank 0 polls).
    # A heartbeat that declines a printer is left out for it. Every PC computes the same answer
    # from the same inputs.

class LocalStore:
    """This user's and this PC's v2 folders: settings, local copy, previews, send-log queue."""
    def __init__(self, paths: HxPaths, *, clock: Clock = SYSTEM_CLOCK) -> None: ...
    def read_user_json(self, name: str) -> dict: ...        # {} if missing or unreadable
    def write_user_json(self, name: str, data: dict) -> None: ...
    def read_local_copy(self) -> HxData | None: ...
    def write_local_copy(self, data: HxData) -> None: ...   # one set, written whole then swapped
    def preview_path(self, sha256: str) -> str: ...
    def queue_send_log(self, name: str, entry: dict) -> None: ...
    def pending_send_logs(self) -> list[tuple[str, dict]]: ...
    def mark_uploaded(self, name: str) -> None: ...

class DataService:
    """The current HxData. A 'hx-data' thread reloads it every data_refresh_s (or on reload()),
    refreshes the local copy after every complete load from the share, falls back to the local copy
    when the share is down, flushes the send-log queue when it can, and posts DataUpdate and
    ShareHealth on the bus."""
    def __init__(self, *, store: ShareStore, local: LocalStore, bus: EventBus, identity: Identity,
                 clock: Clock = SYSTEM_CLOCK, timings: Timings = TIMINGS) -> None: ...
    current: HxData | None       # read from any thread (one attribute swap)
    def start(self) -> None: ...
    def stop(self, timeout_s: float = 2.0) -> None: ...
    def reload(self) -> None: ...                           # non-blocking
    def save(self, record: Record, *, expected_revision: int | None) -> Record: ...
        # blocking (worker thread only): revision+1, write, admin-change log, reload.
        # Raises RevisionConflict, ShareUnavailable, NotAllowed (not an administrator)
    def save_file(self, film_id: str, kind: str, source_path: str) -> FileRef: ...   # copies into templates
    def can_send(self) -> tuple[bool, Msg | None]: ...     # the 24-hour rule
    def append_send_log(self, entry: dict) -> tuple[str, bool]: ...
        # blocking: writes to logs\hx-sends, or queues it on this PC when the share is down;
        # returns (file name, queued)
    def find_send(self, send_id: str) -> dict | None: ...  # blocking: the send's log entry, share then queue
    def pending_logs(self) -> int: ...                     # queued entries ("N log entries waiting")
```

### 6.7 hx_send (owner: send.md)

All three classes run on a worker thread (never the Tk thread), post events through `on_event`
(the UI passes `bus.post`), and talk to printers only through `ClientPool`. Each machine of a Send
runs on its own thread; `on_event` is called from several threads at once.

```python
class SendPipeline:
    def __init__(self, *, store: ShareStore, data: DataService, clients: ClientPool, time: TimeSource,
                 identity: Identity, clock: Clock = SYSTEM_CLOCK, timings: Timings = TIMINGS) -> None: ...
    def run(self, plan: SendPlan, on_event: Callable[[object], None], *,
            cancel: CancelToken | None = None) -> SendResult: ...
        # Steps STEP_CHECK_CHOICE .. STEP_CONFIRM_2 for every machine; posts SendEvent for each
        # step and SendFinished at the end; writes expected states, line statuses and the send log.
        # The countdown is the pipeline's: STEP_LOCK takes the locks and waits (cancellably) until
        # timings.countdown_s has passed since its "running" event, while STEP_WAIT_POLLER's wait
        # overlaps it; the UI only shows the seconds left from that event's `at`.
        # cancel works only before the first printer request; after that it is ignored.
        # Never raises for a machine's failure (that becomes a red MachineResult); raises only
        # PlanError (nothing sent) and NotAllowed (not a sender).
    def confirm(self, send_id: str, machine_ids: list[str], everything_right: bool) -> None: ...
        # the sender's answer: confirmation log entry, expected state and line status
        # (only where expected.send_id still equals send_id). Raises NotAllowed if not the sender.
    def leave_it(self, send_id: str, machine_ids: list[str]) -> None: ...
        # keeps them red, logs who and when, and sets a new reread_token
    def record_offline(self, entry: dict) -> list[str]: ...
        # after an outage: writes the expected state of a Send made without the share (send.md §14.1);
        # returns the machine ids it wrote

class PrinterActions:
    def __init__(self, *, store: ShareStore, data: DataService, clients: ClientPool, time: TimeSource,
                 identity: Identity, clock: Clock = SYSTEM_CLOCK, timings: Timings = TIMINGS) -> None: ...
    def stop(self, machine_ids: list[str], on_event: Callable[[object], None], *,
             reason: str = "user", send_id: str = "", action_id: str = "") -> ActionResult: ...
        # reason "user" | "rejected"; action_id "" means new_id("ac"); the UI passes its own (§7.1)
    def check_resume(self, machine_id: str) -> ResumeCheck: ...
    def resume(self, machine_ids: list[str], on_event: Callable[[object], None], *,
               action_id: str = "") -> ActionResult: ...
    def set_clocks(self, machine_ids: list[str], on_event: Callable[[object], None], *,
                   action_id: str = "") -> ActionResult: ...

class TestPrintRun:
    def __init__(self, *, store: ShareStore, data: DataService, clients: ClientPool, time: TimeSource,
                 identity: Identity, plan: TestPlan, clock: Clock = SYSTEM_CLOCK,
                 timings: Timings = TIMINGS) -> None: ...
    def run(self, on_event: Callable[[object], None], *, cancel: CancelToken | None = None) -> TestResult: ...
        # holds the printer lock for the whole run; cancel stops at the next safe point and then
        # always runs the end procedure (printing off, both slots emptied, "Stopped, needs a Send")
    def end_again(self, on_event: Callable[[object], None]) -> TestResult: ...
        # only the end procedure, under a new lock: the administrator's Try again when printing
        # couldn't be confirmed off (send.md §15.5)
```

### 6.8 hx_poller (owner: states.md)

```python
class Poller:
    """The shared polling service: a coordinator thread 'hx-poller' plus one worker thread
    'hx-poll-<printer_id>' per printer this PC currently polls (§7.1)."""
    def __init__(self, *, store: ShareStore, data: DataService, clients: ClientPool, time: TimeSource,
                 bus: EventBus, identity: Identity, version: str, clock: Clock = SYSTEM_CLOCK,
                 timings: Timings = TIMINGS, local: LocalStore | None = None) -> None: ...
        # local: for the reach memory (storage.md §12.3); None in tests (an in-memory one that can poll)
    def start(self) -> None: ...
    def stop(self, timeout_s: float = 2.0) -> None: ...
        # hands each polled printer to the next ranked poller (yielded_to), removes the heartbeat
    def set_monitor_open(self, monitor_open: bool, floor_board: bool) -> None: ...   # from the UI
    def refresh_now(self) -> bool: ...     # standalone mode; False if used within refresh_now_min_s
    mode: str                    # "share" | "standalone" | "idle"
    polling: list[str]           # printer ids
    def local_statuses(self) -> dict[str, PollStatus]: ...   # this PC's own latest readings, by machine

class StatusWatcher:
    """'hx-watch' thread: every watch_s reads the machine, line, lock and ack files through the store
    (or, in standalone mode, the poller's local_statuses), builds a BoardView with hx_state and posts
    BoardUpdate."""
    def __init__(self, *, store: ShareStore, data: DataService, poller: Poller, bus: EventBus,
                 identity: Identity, clock: Clock = SYSTEM_CLOCK, timings: Timings = TIMINGS) -> None: ...
    def start(self) -> None: ...
    def stop(self, timeout_s: float = 2.0) -> None: ...
    def refresh_now(self) -> None: ...
    latest: BoardView | None
```

### 6.9 hx_fake (owner: protocol.md). Tests and manual runs only.

```python
class FakePrinter:
    """An Hx printer on 127.0.0.1: Autopilot on POST /, the web login, Load and sme_repeat with one
    shared reply buffer, and the real printer's bad habits, each switchable."""
    def __init__(self, *, uid: int = 1109, password: str = "123321", placement: str = "delay",
                 fonts: tuple = STANDARD_FONTS, firmware: str = "69.75.10", clock: Clock = SYSTEM_CLOCK,
                 latency_s: float = 0.0, mixup: bool = False, busy_on_overlap: bool = True,
                 drop_after_idle_s: float | None = None, editor_page_bytes: int = 2_300_000,
                 editor_stall_s: float = 5.0, seed: int = 0,
                 offset_fn: Callable[[float], float] = local_utc_offset) -> None: ...
        # offset_fn (hx_time's by default) fixes the printer clock's offset at construction; the clock
        # never follows daylight saving or the host's zone (protocol.md §1.2, §15.3)
    host: str                    # "127.0.0.1"
    port: int                    # chosen at start()
    def start(self) -> "FakePrinter": ...
    def stop(self) -> None: ...
    def printer_record(self, **fields) -> Printer: ...
        # a Printer pointing at this fake, its token scrambled with hx_protocol.release_key()
    # state, under self.lock
    lock: threading.Lock
    slots: dict[int, MessageDesign]   # stored messages
    printing: bool
    current_slot: int
    counters: dict[str, int]     # "PMCNT", "MSCNT"
    ink_name: str; ink_level: int; head_temp_c: int
    clock_offset_s: float        # printer clock minus (real time + base_offset_s); TD sets it
    encoder: dict                # {"ENCST": 1, "ENWHD": 56, "ENPPR": 5000, "ENTPM": 1}
    # control
    def print_bags(self, n: int) -> None: ...      # as if the bagger triggered n prints (only while printing)
    def go_silent(self, silent: bool = True) -> None: ...   # stop answering (connections time out)
    def override(self, tag: str, reply: str | Callable[[], str] | None) -> None: ...
    def expire_web_session(self) -> None: ...     # next web request gets the 401 login page
    def leave_in_buffer(self, body: bytes) -> None: ...  # what another client's request left behind
    # observation
    requests: list[dict]         # {"t", "kind", "tag", "body", "reply"} in arrival order
    writes: list[str]            # tags of every write received
    max_in_flight: int           # the most requests ever open at once
    violations: list[str]        # e.g. "two requests in flight", "SAVEMSG to the printing slot",
                                 # "editor page requested", "printer-wide command <TAG>"

class FakeSntp:
    """An SNTP server on UDP 127.0.0.1."""
    def __init__(self, *, offset_s: float = 0.0, silent: bool = False) -> None: ...
    port: int
    def start(self) -> "FakeSntp": ...
    def stop(self) -> None: ...

class FakeWorld:
    """A temp share, fake printers and a fake time server, seeded with example records (Example Co,
    Line 1 Bagger 1, ...) with hx_share.write_record plus a changes.json (protocol.md §16.2)."""
    def __init__(self, tmpdir: str, *, printers: list[dict] = (), clock: Clock = SYSTEM_CLOCK,
                 timings: Timings = TIMINGS) -> None: ...
    share_root: str
    fs: FlakyFileOps
    printers: list[FakePrinter]
    sntp: FakeSntp
    def identity(self, name: str, *, admin: bool = False, sender: bool = True, pid: int = 1) -> Identity: ...
    def store(self, identity: Identity) -> ShareStore: ...   # each simulated PC gets its own store,
                                                             # on its own fs.view() (per-PC caches)
    def close(self) -> None: ...

# python hx_fake.py [--printers N] [--encoder] [--mixup]
#   runs fakes and a seeded temp share for manual UI work, and prints the STRIPES_V2 and
#   STRIPES_HX_* values to start `python stripes.py` against them
```

### 6.10 UI modules (owner: ui.md)

```python
# hx_ui_common
COLOUR_HEX: dict[str, str]       # GREEN -> "#15803d", AMBER -> "#b45309", RED -> "#b91c1c",
                                 # GREY -> "#6b7280", BLUE -> "#2563eb" (the Zebra monitor's colours)
class BagText(tk.Canvas):        # bag text: drawn on a canvas, so the language switch can't touch it
    hx_no_translate = True
    def __init__(self, master, *, size: int = 12, bold: bool = False, wrap_px: int | None = None, **kw): ...
    def set(self, text: str) -> None: ...
    def get(self) -> str: ...
class PictureView(tk.Canvas):    # a read-back picture, scaled to fit
    hx_no_translate = True
    def set_bmp(self, bmp: bytes | None) -> None: ...
def bmp_to_image(bmp: bytes) -> "PIL.Image.Image": ...
def text(msg: Msg | None) -> str: ...   # tr(msg.text, **values) with nested Msg values translated first; "" for None
class HxWindow(tk.Toplevel):     # base of every MapleJet window
    hx_window = True             # the Zebra shortcuts ignore these windows (§10.3)
def tell(parent, msg: Msg, *, kind: str = "info") -> None: ...    # patched in tests
def ask(parent, msg: Msg, *, yes: str, no: str) -> bool: ...      # yes/no are N_ texts
class SyncBackground:            # tests: .run(work, done) calls both at once on the calling thread
    def run(self, work, done) -> None: ...
@dataclass(kw_only=True)
class HxSettings:                # %APPDATA%\Stripes\v2\maplejet.json; ui.md owns the fields
    floor_board_at_startup: bool = False
    floor_board_screen: int = 0
    floor_board_size: str = "full"   # "full" or "<width>x<height>"
    @staticmethod
    def load(local: LocalStore) -> "HxSettings": ...
    def save(self, local: LocalStore) -> None: ...

# hx_ui_send
class SendWindow(HxWindow):
    def __init__(self, master, services: "HxServices", *, prefill: SendRequest | None = None,
                 line_ids: list[str] | None = None): ...
class ResultWindow(HxWindow):    # pictures, origin in large type, Everything is right / Something's wrong
    def __init__(self, master, services, result: SendResult): ...
    @staticmethod
    def from_log(master, services, send_id: str) -> "ResultWindow | None": ...   # reopened from a card
class FailureWindow(HxWindow):   # the full-screen red window: Try again / Leave it
    def __init__(self, master, services, result: SendResult, plan: SendPlan): ...
class StopDialog(HxWindow): ...
class ResumeDialog(HxWindow): ...
class SetClocksDialog(HxWindow): ...

# hx_ui_monitor
class LineBoard(ttk.Frame):      # line and machine cards; embedded in the tab and in MonitorWindow
    def __init__(self, master, services): ...
    def show(self, board: BoardView) -> None: ...
class MonitorWindow(HxWindow):   # "MapleJet monitor"
    def __init__(self, master, services): ...
class FloorBoard(HxWindow):
    def __init__(self, master, services, *, screen: int = 0, size: str = "full"): ...
class AlertSounder:              # rings hx_win.play_alert while sounding_alerts(...) is non-empty
    def __init__(self, services): ...

# hx_ui_admin
class AdminWindow(HxWindow):     # pages: company, lines, machines, printers, products and films, print texts
    def __init__(self, master, services, *, page: str = "", record_id: str = ""): ...
class FilmEditor(ttk.Frame): ...
class PrintTextEditor(ttk.Frame): ...
class TestPrintWindow(HxWindow):
    def __init__(self, master, services, *, film_id: str, machine_id: str = "", kind: str = "test"): ...
class CalibrationWindow(HxWindow):
    def __init__(self, master, services, *, machine_id: str): ...

# hx_ui
class Navigator:                 # how the screens open each other without importing each other
    def open_send(self, *, prefill: SendRequest | None = None, line_ids: list[str] | None = None) -> None: ...
    def open_result(self, send_id: str) -> None: ...
    def open_failure(self, result: SendResult, plan: SendPlan) -> None: ...
    def open_monitor(self) -> None: ...
    def open_floor_board(self) -> None: ...
    def open_admin(self, *, page: str = "", record_id: str = "") -> None: ...
    def open_test_print(self, *, film_id: str, machine_id: str = "", kind: str = "test") -> None: ...
    def stop(self, machine_ids: list[str], *, reason: str = "user", send_id: str = "") -> None: ...
    def resume(self, machine_ids: list[str]) -> None: ...
    def set_clocks(self, line_ids: list[str]) -> None: ...

@dataclass(kw_only=True)
class HxServices:
    version: str
    clock: Clock
    timings: Timings
    bus: EventBus
    bg: object                   # .run(work, done): stripes.Background in the app, SyncBackground in tests
    identity: Identity
    paths: HxPaths
    store: ShareStore            # root None when no share is configured
    local: LocalStore
    data: DataService
    clients: ClientPool
    time: TimeSource
    poller: Poller
    watcher: StatusWatcher
    settings: HxSettings
    nav: Navigator
    def pipeline(self) -> SendPipeline: ...
    def actions(self) -> PrinterActions: ...
    def test_run(self, plan: TestPlan) -> TestPrintRun: ...

def make_services(root: tk.Misc, *, config_path: str | None, bg, version: str,
                  clock: Clock = SYSTEM_CLOCK, timings: Timings = TIMINGS, identity: Identity | None = None,
                  fs: FileOps | None = None, gates: GateRegistry = PROCESS_GATES,
                  key: bytes | None = None) -> HxServices: ...   # builds everything, starts nothing
def start_services(root: tk.Misc, *, config_path: str | None, bg, version: str) -> HxServices: ...
    # make_services + start DataService, Poller and StatusWatcher + the bus pump and the gc timer
    # (§7.1); never touches the share on the calling thread
def stop_services(services: HxServices, timeout_s: float = 2.0) -> None: ...
def can_quit(parent: tk.Misc, services: HxServices) -> bool: ...   # asks if a Send or test is running
def startup(root: tk.Misc, services: HxServices) -> None: ...      # opens the floor board if the settings say so
def open_monitor(root: tk.Misc, services: HxServices) -> None: ...
def language_changed(services: HxServices) -> None: ...            # rebuilds open MapleJet windows' text
def self_check(root: tk.Misc) -> None: ...                         # deploy.md lists the checks
class MapleJetTab(ttk.Frame):
    def __init__(self, master, services: HxServices): ...
    def on_shown(self) -> None: ...
    def on_hidden(self) -> None: ...
```

## 7. Threads and concurrency

### 7.1 Threads

| Thread | Started by | How many | Does | Stops |
|---|---|---|---|---|
| Tk main | `App` | 1 | Every widget, `bus.pump()` every 100 ms, `stripes.Background` results, `gc.collect()` every 2 s | app exit |
| `stripes.Background` workers | UI | short-lived | One-off blocking calls for the UI: admin saves, loading a send log, `check_resume` | when done |
| `hx-data` | `DataService.start` | 1 | Reload HxData, local copy, flush the send-log queue | `stop()` |
| `hx-poller` | `Poller.start` | 1 | Heartbeat every `heartbeat_s`, assignment, start/stop workers, standalone schedule, DST check | `stop()` |
| `hx-poll-<printer>` | `Poller` | 1 per printer polled here | That printer's cycle, one request at a time | assignment change, lock, `stop()` |
| `hx-time` | `Poller.start` | 1 | `TimeSource.now(fresh=True)` every `cache_s`, so the poller's `cached()` reading stays fresh without a worker ever waiting on the time server | `stop()` |
| `hx-watch` | `StatusWatcher.start` | 1 | Read status every `watch_s`, post `BoardUpdate` | `stop()` |
| `hx-send-<id>` | UI | 1 per Send | `SendPipeline.run`: locks, waits, joins machine threads, logs | when done |
| `hx-send-<id>-<machine>` | `SendPipeline` | 1 per machine | That machine's steps | when done |
| `hx-action-<action_id>` | UI (which makes the `action_id` with `new_id("ac")` and passes it in) | 1 per Stop/Resume/Set clocks | `PrinterActions` call | when done |
| `hx-test-<id>` | UI | 1 | `TestPrintRun.run` | when done or cancelled |
| `hx-lock-<printer>` | `PrinterLock.start_keeping` | 1 per held lock | Bump the lock counter every `lock_refresh_s` | `release()` |
| `hx-io` | `ShareStore` | a small pool | File operations, each abandoned after `share_timeout_s` | a stuck one when its OS call returns |
| pystray, SingleInstance | existing | 1 each | unchanged | unchanged |

Rules:

1. Only the Tk thread touches widgets, `PhotoImage`s and Tk variables. Workers hand results back only
   through `bus.post(...)` or `bg.run(work, done)`.
2. Events and results are plain data (dataclasses, `bytes`, `str`). A worker never creates or holds a
   Tk object. Pictures cross threads as BMP bytes and become `PhotoImage`s on the Tk thread.
3. Python's cyclic garbage collector can free a Tk object on whatever thread happens to allocate,
   and Tk then deadlocks (the reason `Background.run` calls `gc.collect()`). With long-lived workers
   that isn't enough, so `start_services` calls `gc.disable()` and schedules `gc.collect()` on the Tk
   thread every 2 s. `make_services` alone (tests) leaves the collector alone.
4. All hx threads are daemon threads named as above. Each loop waits with `clock.sleep(..., cancel)`
   or an Event, so `stop()` returns within `timeout_s` plus at most one request timeout.
5. Shared mutable state lives only in: the `EventBus` queue; `GateRegistry`; `ShareStore` (its own
   lock, `watch` and `local_locks`); `DataService.current` (replaced whole, never edited);
   `PrinterClient` state (changed only while holding the printer's gate); `Poller`'s worker table
   (its own lock); `TimeSource`'s cache (its own lock).
6. `on_event` callbacks from `hx_send` may come from several threads at once; `bus.post` is safe.

### 7.2 One request at a time: inside one process

1. `GateRegistry` gives each printer (`host:port`) one `threading.Lock`. `PrinterClient` holds it for
   each HTTP exchange, and for the whole of `load_slot` (the Load and its repeats), because a request in
   between could replace the printer's shared reply buffer. All `PrinterClient`s of a process use the
   same registry, so the poller and a sender on one PC take turns per request.
2. Waiting for a gate is bounded by `gate_wait_s`; a timeout raises `PrinterBusy`.
3. `ShareStore.local_locks` records every `PrinterLock` this process holds, including `local_only`
   ones when the share is down. The in-process poller checks it before every request, so a Send or
   test on this PC is never interleaved with this PC's polling, share or no share. A poller request
   already waiting for the gate when the lock is taken may still go out first, within its deadline
   (protocol.md §9 item 2); the holder's own requests then wait for it at the gate.
4. A test asserts `FakePrinter.max_in_flight == 1` with many threads using one `ClientPool`.

### 7.3 One request at a time: across PCs

Two mechanisms, both through the share. storage.md specifies the files and the formula; the rules are:

1. **Single assigned poller.** Every PC writes a heartbeat. Every PC computes `assign()` from the live
   heartbeats (counter moved within `stale_s`, same `FORMAT`, `can_poll`) and the active printers.
   Rank 0 polls. A PC at rank 1 starts polling a printer only when that printer's `poll.json` counter
   has been unchanged for `takeover_s[0]` (15 s) on its own clock; rank 2 after `takeover_s[1]` (20 s);
   rank n > 2 after 20 + 5 × (n − 2) s. A poller re-checks its rank before every cycle. A PC is left
   out of `assign()` for the printers its heartbeat lists in `declined`, as `can_poll` leaves out a whole
   PC: its own worker for that printer is stuck (states.md §6.2 item 3.0), so the next PC polls it and
   nobody hands it back.
2. **Handover.** A poller that is polling a printer but is no longer rank 0, while rank 0 is live,
   finishes its current request, writes `yielded_to = <rank 0 key>` and stops. Rank 0 starts only when
   `poll.json` names itself as writer, names it in `yielded_to`, is missing, or has been stale for
   `stale_s`. A cleanly stopping poller yields each printer to its next-ranked live poller. Rank 0 and
   rank 1 can start in the same moment (both wait 15 s), so every start re-checks its rank from fresh
   heartbeats after the settle and before its first request, and a non-rank-0 starter yields there
   (storage.md §12.6 rule 3, states.md §6.3 step 2).
   The invariant behind rules 1 and 2: **no PC starts polling a printer whose `poll.json` is fresh and
   written by another PC, unless that writer has yielded to it.** So two pollers never overlap, even
   while PCs briefly disagree about who is live. A worker whose last good `poll.json` write is
   `stale_s / 2` or more old (its PC slept, or it was stuck) reads `poll.json` before any write: it never
   writes over a newer one by another PC, and it adopts a newer one yielded to it. A `poll.json` whose
   counter isn't above the poller's own last write (equal when it lands before the new poller's second
   write), written long ago by a PC that isn't live, is a late write by a superseded owner and is
   ignored (states.md §6.4 step 7.2). A write that took `stale_s / 2` or more stops share mode.
3. **Printer lock.** Sends, test prints, calibration, Stop, Resume and Set clocks take the printer's
   lock (`O_CREAT | O_EXCL` on the share). The holder bumps its counter every `lock_refresh_s`.
   Another PC treats a lock as abandoned only after its counter has been unchanged for `lock_stale_s`
   (60 s) on that PC's own monotonic clock, and takes it over by renaming it aside first (only one
   rename can win). Locks and acks are read by opening the file by name, never by listing the folder,
   because Windows can hide a new file in a listing for several seconds.
4. **Poller steps aside.** The assigned poller reads the printer's lock before every single request.
   If it is held, the poller makes no request to that printer and writes `paused_for_lock = <token>`.
5. **Sender waits.** Before its first request to a printer, a lock holder waits until that printer's
   `poll.json` shows `paused_for_lock` equal to its token, or its counter has been stale for `stale_s`,
   or there is no `poll.json` for that machine. The countdown usually covers the wait.
6. **The poller's one write.** The automatic clock set after a daylight-saving change is made only by
   the assigned poller, only while no lock is held (checked right before each request), once per
   change and printer, never repeated, logged to `hx-events` (states.md §6.11). It doesn't take a
   lock: it is already the only PC talking to that printer.
7. **Share down.** No lock can be taken (`local_only`) and pollers can't coordinate. That PC's Send
   still checks and confirms each printer but every machine is amber (plan wording). In standalone
   mode only PCs with a MapleJet monitor or the floor board open poll, every `standalone_poll_s` at a
   random offset, quick items only, and only once the share has been unreachable for `lock_stale_s`
   without a break (states.md §6.12). **Residual risk (owner sign-off, §12.1):** when only some PCs
   lose the share (a broken SMB session on one PC), that PC's standalone checks can overlap the
   share-mode pollers and lock-holding senders on the other PCs for as long as its outage lasts, and
   those senders get no `no_lock` warning. The overlap is limited to the quick reads every 30 s; web
   read-backs stay safe because they carry request numbers, and a mixed-up Autopilot reply can only
   turn a result red (a false alarm), never pass a wrong message.
8. **Holding a lock is bounded in time.** A holder never starts a printer call that could still be
   running when another PC may judge its lock stale: `PrinterLock.check(budget_s)` before every call,
   with the call's worst case from `hx_protocol.worst_case_s` (storage.md §11.3, protocol.md §8.3), and
   every call made under the lock carries the lock's lease end (`PrinterLock.lease_end()`: last good
   refresh + `lock_stale_s / 2`) as its deadline (send.md §10.5). The client checks that deadline after
   the gate and before every exchange, so a call that passed its check and then waited (this PC's gate,
   a frozen PC) is refused, nothing sent. The lease is counted from the time taken just before the share
   write that made or refreshed the lock file, never from when it returned, and a write that took
   `lock_stale_s / 2` or more makes the lock lost (storage.md §11.2, §11.3); a share write the caller gave
   up on never commits, and a lock create it gave up on removes its file while that is still safe
   (storage.md §8.1); and a release only removes the file while the lease still runs, and never this
   PC's own new lock on the printer (storage.md §11.5). The same applies to the poller and its own `poll.json`: it never starts a request that could still
   be running `stale_s / 2 + http_timeout_s` (9.5 s) after the start of its last successful `poll.json`
   write (states.md §6.4 step 7), and every poller request, a second reading's login included, carries
   that time as its deadline, so it never waits for this PC's gate past it either (protocol.md §9
   item 2).

Residual risks of rule 8 and the invariant: a printer can't refuse an old owner, so a PC that freezes
in the microseconds after its last deadline check can still send one late request (at most one mixed-up
reply, which TN/XID, the per-tag checks and the two-reading rule turn into a false alarm, never a false
green); a share write can land late (SMB has no compare-and-swap; the new poller ignores it and rewrites
`poll.json` at its next tick, so a board shows the old one for at most one tick); and a stuck thread
can't be killed: it keeps this PC's gate for that printer, so this PC's own Sends to it get
`PrinterBusy` (fails safe).

Named residual risks of the printer lock (storage.md §11.8 lists all six; they need the owner's
sign-off):

- **One late request after a freeze.** A PC that freezes in the instant between the client's last
  deadline check and the request leaving sends that one request when it wakes, maybe after another PC
  took the printer over. The freeze must last about 25-30 s. Bound: one exchange per frozen thread, then
  `LockLost`; the new holder's read-backs and the poller's drift rule turn it red (a message switch in
  about 7-12 s, a same-name content edit at the slow Load, about 60-65 s).
- **Platform assumption: `time.monotonic()` counts sleep.** On Windows, Python 3.12's monotonic clock is
  `GetTickCount64`, which counts sleep and hibernation, so a woken holder fails its check. A VM pause the
  guest's clock doesn't see would let it pass until its keeper's next read (5 s plus one read of
  overlap). Check it on the test PC (`time.get_clock_info("monotonic")`, a sleep and resume with a lock
  held).
- **Plant rule: floor PCs never sleep or hibernate while Stripes runs** (power policy), and aren't VMs
  that can be paused. The first two items depend on it.
- **A share call already issued can still land** (a replace, rename or remove under way when the caller
  gave up): for the lock, liveness only (the new holder stops, the printer looks locked for 60 s).

Sequence of a Send's first request to a printer:

1. Sender: `lock.acquire()` creates `status\locks\<printer>.lock`, then `start_keeping()`.
2. Poller on whichever PC: before its next request it opens the lock file, sees it, makes no request,
   writes `paused_for_lock = token` in `poll.json` on its next status write (at once).
3. Sender: reads `poll.json` until it sees its token (or staleness, or no file), and only then sends
   its first request.
4. Sender: `lock.release()` at the end. The poller resumes on its next cycle.

### 7.4 Time and freshness

1. Every staleness rule (15 s status and heartbeats, 15/20 s takeover, 30 s NOT MONITORED, 60 s lock)
   is judged by watching a counter change on the reader's own `clock.monotonic()` (`CounterWatch`).
   Other PCs' timestamps are never compared with this PC's clock (except the long-gap and first-sight
   rules for `checking`, states.md §6.3 step 1 and §7 item 6). NOT MONITORED and "checked N s ago" use
   `poll.json`'s `judged` counter, which only judgements and paused writes move (states.md §6.14), so a
   takeover's bookkeeping never makes an old judgement look fresh; an older file without it uses its
   counter. Takeover, handover and the sender handshake use the `poll.json` counter.
2. A counter seen for the first time is fresh (age 0). So a PC that starts while a holder has crashed
   waits the full 60 s before taking over its lock.
3. Wall-clock times (`clock.time()`, `TimeReading`) are for display, logs and the printers' clocks only.

### 7.5 Cancellation

1. A `CancelToken` is checked between steps and before each request, and is passed to every
   `clock.sleep`. No thread is ever killed; blocking calls are bounded (printer requests by
   `http_timeout_s`, share operations by `share_timeout_s`, SNTP by its timeout), except local-disk I/O
   and a bug. So the poller's coordinator watches each printer it ranks first for: a worker with no good
   `poll.json` write for `not_monitored_s` (30 s) is fenced (it never sends or writes again), logged as
   `internal_error` `worker_stuck`, and its printer declined in the heartbeat (§7.3 rule 1) until the
   thread has ended and the decline's time has passed: `lock_stale_s`, doubled for each fence in a row,
   up to 15 minutes. A printer whose first worker after a decline fails again is declined again at once
   (states.md §6.2 item 3.0).
2. Send: cancel works only until the first printer write (the countdown). Then the UI removes Cancel
   and the pipeline ignores the token, because stopping halfway leaves the printer in an unknown state.
3. Test print: cancel (Stop test) stops after the current request, then always runs the end procedure.
4. Poller and watcher: `stop()` sets their token; workers finish the request in flight and exit.
5. Quitting during a Send or test: `can_quit` asks first. If the person quits anyway, the locks go
   stale after 60 s and the poller handles it as an interrupted Send (states.md).

## 8. Error model

1. Expected failures are `HxError` subclasses (§5.2). Anything else is a bug: it is caught at the top
   of each worker thread, printed with its traceback, logged to `hx-events` as "internal error", and
   the machine it was working on becomes red at the current step. A worker thread never dies silently.
   A poller worker that can't make progress (stuck in a call, or every `poll.json` write failing) is
   noticed by the coordinator's watchdog and its printer handed to the next PC (§7.5 item 1).
2. Non-UI code never shows text. It raises an `HxError` or returns a `Msg`; the UI calls
   `tr(msg.text, **msg.values)`. Logs store `msg.english()` and the `Msg` itself.
3. Reads may be retried once automatically (`read_retry_s`) after `NoReply`, `PrinterBusy` or a
   failed sense check. Writes are never repeated without the operator.

How failures become results and machine states (send.md fills in the texts):

| Where | Exception | What happens | Result | Expected state written |
|---|---|---|---|---|
| Before anything | `NotAllowed` | Send refused, nothing done | none | none |
| `check_choice` | `PlanError` | Whole Send refused, nothing done | none | none |
| `lock` | `LockHeld` | That line's machines not sent: "Line 1 is being sent by jsmith" | red, not sent | none |
| `lock` | `ShareUnavailable` | Carries on with `local_only` locks | amber note `no_lock` | none (the share can't be written) |
| `check_machine` | `WrongPrinter`, fonts differ, `PrinterError` | Machine stops; still printing its previous message | red | `failure`, mode unchanged |
| `check_machine` | ink colour mismatch | Machine skipped and flagged | red, note `ink_mismatch` | `failure` |
| `set_clock` | `TimeServerError` (inside `TimeSource`) | PC time used, warning | amber note `pc_time` | `notes` |
| `set_clock`, `build` | `PrinterError`, `CommandRefused` | Machine stops; previous message still printing | red | `failure` |
| `verify_idle` | `CheckFailed`, `WebReadFailed` | Machine stops before switching | red | `failure` |
| `switch`, before PRINTMSG | the "switch pending" state can't be written with a `held` lock, or the switch entry can't be saved even on this PC | Machine stops before switching (send.md §10.9) | red | `failure` when it can be written |
| `switch`, `confirm_1`, `confirm_2` | any | Machine stops; "may be printing the new message, the old one, or nothing" | red, `after_switch` | `failure` with `after_switch`, mode `switch_pending` kept |
| all passed | none | Live, waiting for the sender | amber "Send not confirmed" | mode `running`, `confirmed` False |
| `operator_confirm` | "Everything is right" | | green | `confirmed` True |
| `operator_confirm` | "Something's wrong" | Stop dialog follows | red | `rejected`, `resume_blocked` (no `failure`: states.md judges it from `rejected`) |
| PC crash mid-Send | lock goes stale | Poller reads the running message | red "Send interrupted on PC X" (old) or amber "Send interrupted, not confirmed" (new) | written by nobody; the poller judges from `switch_pending` |

Poller-side failures: `NoReply` counts towards `unreachable_after`; `BadReply` and mixed-up web replies
count towards "Something else is talking to printer X"; `AuthFailed` and `WrongPrinter` become red
machine states; `ShareUnavailable` switches the poller to standalone mode. states.md maps each to codes
and decides who is told.

## 9. Testing

All tests use `unittest`, match the existing test files' style and run with
`xvfb-run -a /opt/stripes-venv/bin/python -m unittest -v` (Python 3.12, Tk, Pillow, segno) on Linux
and `python -m unittest -v` on `windows-latest` in CI. The full suite should stay under about two
minutes: no test sleeps more than 0.5 s of real time; multi-threaded tests use `TIMINGS.scaled(...)`
and logic tests use `FakeClock`.

### 9.1 Fakes

| Fake | Where | What it gives |
|---|---|---|
| Fake printer | `hx_fake.FakePrinter` | A real HTTP server on 127.0.0.1 (`http.server.ThreadingHTTPServer`, its own port). Autopilot queries and writes; message design (CRTMSG, CTEXT, CTDAT, CPPARM, SAVEMSG, PRINTMSG, PRINT, TD); the web login and the cookies; Load and `sme_repeat` with **one shared reply buffer** (first reply = previous result, possibly another client's or SAVEMSG's); **mixed-up replies** when two or more requests are in flight (`mixup`), busy (7) at overlap; **first request after idle dropped** (`drop_after_idle_s`); **401** with a gzipped login page without a valid token or after `expire_web_session`; the **slow 2.3 MB editor page** that stalls every other request; **encoder and delay modes** (ENCST, DLYMM in mm vs DELAY in µs, DELAY query reporting ms); size read back one lower; defaults for unset print settings (speed 1000, delay 0, angle 30); counters that rise with `print_bags`; a clock with an offset; `violations` for anything Stripes must never do |
| Fake SNTP | `hx_fake.FakeSntp`, and `TimeSource(query=...)` | A time server with an offset, or silent |
| Fake share | `FakeWorld.share_root` (a temp folder) plus `hx_share.FlakyFileOps` | The real file code on a temp folder; `down`, `slow_s` and `hide_new_s` fake a dead, slow or listing-cached share; `view(down=..., slow_s=...)` cuts off or slows one simulated PC alone (storage.md §16) |
| Fake clock | `hx_core.FakeClock` | Deterministic time for single-threaded logic: lock staleness, takeover, 24-hour rule, DST |
| Scaled timings | `TIMINGS.scaled(f)` | The real threads at 1/f of the real waits |
| Fake identity | `FakeWorld.identity(...)` or `Identity(...)` | Senders, administrators and plain users; several PCs in one process |
| Separate gates | `GateRegistry()` per simulated PC | So a multi-PC test checks the share lock and assignment, not the in-process gate |
| Parser fixtures | `testdata/hx/` | The sanitized captures from the real printer (slot 9 Load, compact slot 0 Load, stale Save reply, empty slot) |

### 9.2 Test files

| File | Covers |
|---|---|
| `test_hx_core.py` | `FakeClock` both modes, `CancelToken`, `EventBus` order, subscriber errors, unsubscribe during pump |
| `test_hx_model.py` | `to_json`/`from_json` round trip of every dataclass, unknown keys ignored, the §5.3 type rules one by one (an int for a float field, a bool refused, lists back to tuples, None only for optional fields), `Identity.key` and roles, `Timings.scaled` |
| `test_hx_message.py` | Origin phrase and origin-word refusal (case, punctuation, aliases); validation; `merge_plan`; print position incl. the wrap example (610, 150, 412 → 348) and the film-stop warning; delay in µs; message name length and check letter; golden `build_commands` output for encoder and delay machines; `compare` (size one lower, defaults Autopilot can't set, date text ignored); fingerprint stability; test steps and labels; head distance |
| `test_hx_state.py` | The state table from states.md row by row; second readings; line worst colour; NOT MONITORED after 30 s; alert recipients and acks; floor-board rows; which alerts sound for whom |
| `test_hx_protocol.py` | Parser on every fixture (CRLF, the 0xFF byte); login and re-login after 401; repeat until its own TN/XID, stale buffer from another client and from SAVEMSG; give-up after 5 repeats or 3 s; idle drop retried for reads; warm-up read before a write; a write never retried; whitelist and forbidden queries raise `ForbiddenCommand`; error codes never pass as values; `max_in_flight == 1` with 8 threads; the editor page is never requested; token scramble round trip and wrong key |
| `test_hx_time.py` | SNTP against `FakeSntp`; fallback to PC time; cache; `recent_dst_change` with an injected offset function; the TD command |
| `test_hx_share.py` | Whole-file write and swap; add-only logs with the completion marker, readers skip unfinished files; lock create, refuse, refresh, stale takeover after 60 s (`FakeClock`), lost lock; heartbeats and the live set; `assign` stable, balanced and in takeover order; local copy and the 24-hour rule; the send-log queue and upload markers; share timeout with `FlakyFileOps(down=True)`; `FormatTooNew` |
| `test_hx_share_windows.py` | `skipUnless(sys.platform == "win32")`: a reader holding a file open during a swap; add-only folders; `open_for_read` |
| `test_hx_send.py` | Full Send on a delay and an encoder fake: commands, three read-backs, expected states, log; failure injected at every step → the result and expected state in §8; Try again; Leave it; confirm by someone else refused; Stop, Resume (refused after a rejection or a changed message), Set clocks; test print with counted and timed steps, the 60 s no-print stop, the end procedure; calibration; `writes ⊆ ALLOWED_WRITE_TAGS`; never SAVEMSG to the printing slot while printing; never `SETPPARAM` |
| `test_hx_poller.py` | Request budget under 1 request/s per printer; pause for a lock and resume; drift needs a second reading; unreachable after 3 failures and the backoff; standalone mode; the DST clock set fires once, only without a lock |
| `test_hx_sim.py` | Several simulated PCs (own identity, store, pool, gates) on one share with several fakes, `mixup` on: PCs drop out and return, printers switched off, a Send from two PCs at once, a slow printer with a slow share and a silent time server, one PC cut off from the share alone. Asserts: every fake's `max_in_flight == 1` (in the one-PC-cut-off case, which §7.3 rule 7 accepts as a residual risk, that and the "two requests in flight" violation are replaced by: every Send's read-backs matched their own TN and XID), no `violations`, one writer per `poll.json` at a time, takeover within 15–20 s (scaled), NOT MONITORED when every poller is gone |
| `test_hx_ui.py`, `test_hx_ui_send.py`, `test_hx_ui_admin.py` | Under xvfb (skipped if Tk can't start), split by module (ui.md §17): the MapleJet tab builds; the Send window flow against fakes (bus pumped by hand, dialogs patched); result and red windows; monitor and floor board render a `BoardView`; an admin save |
| `test_hx_win.py` | the non-Windows fallbacks of every `hx_win` function, the identity overrides (unfrozen only), and `open_for_read`; the Windows calls themselves run in `test_hx_share_windows.py` and the self-check |
| `test_i18n.py` (changed) | `SOURCES` also lists every `hx_*.py` in the repo folder except `hx_fake.py` and the generated `hx_key.py`, found by listing the folder (so the test covers each module as soon as it exists); the test imports them so their catalogues are registered; `add_spanish` refuses a conflicting pair |
| `test_stripes.py` (changed) | The v2 paths, the one-time settings copy, the deployment-log folder, the Zebra config read from `v2\config` with the fallback; Ctrl+P ignored on the MapleJet tab and in MapleJet windows; bag text unchanged after switching to Spanish and running `App._retranslate` (ui.md §17.2) |
| `test_make_version.py` (changed) | deploy.md §6.1: `parse_key`, `stamp_key`, `key_id` equal to `hx_protocol.key_id`, `main` |
| `test_deploy.py` (new) | deploy.md §6.2: `.gitignore`, no `hx_key.py` in the checkout, the app never imports `hx_fake`, the spec's excludes, the app imported with the exe's exclusions blocked, the self-check from source, `check_release_key`, `version_text` |
| `test_folder_script.py` (delivered after development) | deploy.md §8.8, Windows only |

### 9.3 Keeping the GUI testable

1. UI modules hold no rules. What a window offers, enables, colours or warns about comes from
   `hx_message` and `hx_state` functions, which are tested without Tk.
2. Every window takes `HxServices`. Tests build them with `make_services(root, ...)` and fakes:
   `FakeWorld` for the share and printers, `SyncBackground`, `FakeClock` or scaled timings, an
   explicit `Identity`, `release_key()` (the development key in a source checkout), and a fresh
   `GateRegistry`.
3. Nothing starts threads in a constructor. Tests call `bus.pump()` and `root.update()` themselves.
4. `hx_ui_common.tell` and `ask` are the only dialogs; tests patch them.
5. Each window keeps its main widgets and Tk variables as attributes (ui.md lists them), so tests can
   fill fields and `invoke()` buttons.
6. One withdrawn `tk.Tk()` per test class, destroyed in `tearDownClass`.

## 10. stripes.py integration and paths

This is the complete list of changes to `stripes.py` and `i18n.py`, including the three key hooks
deploy.md §1.3 specifies (§10.6 item 5). ui.md describes what appears inside the MapleJet tab; it
doesn't add hooks. Every `stripes.py` change here is written by WP9 and every `i18n.py` change by WP1
(README.md "Work packages").

### 10.1 Start-up order in `App.__init__`

1. As today up to `self.bg = Background(self)`.
2. `self.hx = hx_ui.start_services(self, config_path=configured_folder(), bg=self.bg, version=VERSION)`.
   It returns at once and never touches the share on the Tk thread. Services start even when Stripes
   starts hidden in the tray (`--tray`), because every PC running Stripes takes part in polling.
3. `self._build()` (with the tab split below).
4. As today: printer list, shortcuts (gated), tray, deployment log.
5. Last: `hx_ui.startup(self, self.hx)`, which opens the floor board when the user's settings say so.

### 10.2 The tab split

1. The header (logo, title, language switch, version) and the separator under it stay in `outer`.
   ui.md may change the subtitle text.
2. Below them: `self.top_tabs = ttk.Notebook(outer)`, packed `fill="both", expand=True`.
3. `self.zebra_tab = ttk.Frame(self.top_tabs)` is added with the text `"  Zebra  "`; the printer row,
   stock row, `body`, `bottom` and `status` that `_build` packs into `outer` today are packed into
   `self.zebra_tab` instead, unchanged (including `bottom` packed `side="bottom", before=body` and
   `status` packed `side="bottom", before=bottom`, as `stripes.py` does today).
4. `self.hx_tab = hx_ui.MapleJetTab(self.top_tabs, self.hx)` is added with the text `"  MapleJet  "`.
   Both tab texts are names, so they aren't passed through `tr()`.
5. `self.top_tabs.bind("<<NotebookTabChanged>>", ...)` calls `self.hx_tab.on_shown()` or
   `on_hidden()`.
6. The Zebra `PrinterMonitor`, `PrinterDialog` and `DeploymentWindow` are unchanged.

### 10.3 Shortcuts

`Ctrl+P` and `Ctrl+Enter` (`bind_all` in `App.__init__`) go through one guard:

```python
def _zebra_shortcut(self, event, action):
    """The Zebra shortcuts only work on the Zebra tab, and never in a MapleJet window."""
    try:
        if getattr(event.widget.winfo_toplevel(), "hx_window", False):
            return
    except (AttributeError, tk.TclError):
        pass
    if self.top_tabs.select() == str(self.zebra_tab):
        action()
```

`self.bind_all("<Control-p>", lambda e: self._zebra_shortcut(e, self.print_label))`, and the same for
`<Control-Return>`. The Simple box's own `<Control-Return>` binding is inside the Zebra tab and stays.

### 10.4 Language switch and bag text

1. `App._retranslate` starts with
   `if getattr(widget, "hx_no_translate", False): return` (don't touch it or its children).
2. Bag text is shown only in `BagText`, `PictureView`, Entry/Text contents, Combobox values and
   Treeview cells, never as the `text` of a Label, Button, LabelFrame, Checkbutton, Radiobutton or
   Notebook tab, a Treeview heading or a window title (all of which `_retranslate` rewrites).
3. `_language_changed` ends with `hx_ui.language_changed(self.hx)`.
4. The bag-text test in `test_hx_ui.py` covers the on-screen previews in Spanish.

### 10.5 Paths

| What | Today | This version | Override (env) |
|---|---|---|---|
| Zebra company config | `ConfigPath` | `v2\config` (needs `printers.json` there), else `ConfigPath` read-only | `STRIPES_CONFIG` (the ConfigPath) |
| v2 on the share | none | `dirname(normpath(ConfigPath))\v2`, the sibling of today's `config` | `STRIPES_V2` |
| Deployment log | `<parent of ConfigPath>\status\*.json` | `v2\status\deployment\` (written); the Deployment status window also reads today's folder, read-only | `STRIPES_STATUS` |
| Zebra per-user settings | `%APPDATA%\Stripes\settings.json` | `%APPDATA%\Stripes\v2\settings.json`, copied once from the old file when missing | `STRIPES_SETTINGS` |
| MapleJet per-user settings | none | `%APPDATA%\Stripes\v2\maplejet.json` | via `STRIPES_V2_ROAMING` |
| Zebra config cache | `%LOCALAPPDATA%\Stripes\config-cache` | `%LOCALAPPDATA%\Stripes\v2\config-cache`, copied once from the old folder (keeping each file's time) when it has no `printers.json` | `STRIPES_CACHE` |
| Single-instance file | `%LOCALAPPDATA%\Stripes\instance.txt` | `%LOCALAPPDATA%\Stripes\v2\instance.txt` (follows `cache_folder()`) | via `STRIPES_CACHE` |
| Local copy, previews, reach memory | none | `%LOCALAPPDATA%\Stripes\v2\hx\` | `STRIPES_V2_LOCAL` |
| Send-log queue (all the PC's users) | none | `%PROGRAMDATA%\Stripes\v2\send-queue\` | `STRIPES_V2_MACHINE` |

`hx_share.roaming_dir()` is `STRIPES_V2_ROAMING` or `%APPDATA%\Stripes\v2` (`~/.config/Stripes/v2`
without APPDATA); `local_dir()` is `STRIPES_V2_LOCAL` or `%LOCALAPPDATA%\Stripes\v2` (`~/.cache/Stripes/v2`);
`machine_dir()` is `STRIPES_V2_MACHINE` or `%PROGRAMDATA%\Stripes\v2` (`<local_dir()>/machine` without
PROGRAMDATA). `v2_share_root(config_path)` is `STRIPES_V2` if set, else
`os.path.join(os.path.dirname(os.path.normpath(config_path)), "v2")`, else `None`.

Changes in `stripes.py`:

1. `settings_path()`: `os.path.join(hx_share.roaming_dir(), "settings.json")` unless `STRIPES_SETTINGS`.
   `App.__init__` calls `hx_share.copy_once(<old settings path>, settings_path())` before
   `load_settings()`, only when `STRIPES_SETTINGS` isn't set.
2. `cache_folder()`: `os.path.join(hx_share.local_dir(), "config-cache")` unless `STRIPES_CACHE`.
   Right after the settings copy, `copy_old_cache()` copies today's `config-cache\stock.json` and then
   `printers.json` into it with `hx_share.copy_once` and sets each copy's modified time to the old
   file's (`load_cached_config()` shows it as "saved {when}"). Only when `STRIPES_CACHE` isn't set and
   the new folder has no `printers.json`, so this version's own copy is never added to or replaced.
   Without it the first start would have no saved copy: no company printers while the server is
   unreachable, and the printer used last (a company one) not selectable before the server is read.
3. New `zebra_config_folder()`: `v2\config` when `v2\config\printers.json` exists, else
   `configured_folder()`. It touches the share, so only `load_company_config()` (a background call)
   uses it; `App.config_folder` stays `configured_folder()`.
4. `status_folder()`: `os.path.join(v2_root, "status", "deployment")` unless `STRIPES_STATUS`.
   `DeploymentWindow` lists `read_deployment()` of that folder plus today's
   `<parent of ConfigPath>\status`, keeping the newest record per computer and user. `App.__init__`
   builds `DeploymentLog(folder, old_folder=old_status_folder())`. `started()` decides, under the
   lock, from the one read of this PC and user's record that its write uses. When there is no record
   in the new folder yet, or only one with no `version` (the heartbeat or the config write made it
   because the share wasn't there at the start), it reads their record in today's folder (same file
   name), so the upgrade is logged as "Updated v1.4.0 -> v2.0.0" with `last_update`, and the new record
   keeps `first_seen` and the events. Without this, every upgraded PC showed as a fresh install. A
   record that can't be read (an `OSError` other than "no such file") isn't written at all, by any
   write, so a share hiccup never logs a false update or loses the history. A damaged record starts
   again without today's file. Today's folder is never written.
5. Today's version reads none of the new places, so old and new copies can run side by side during
   rollout without seeing each other's files.

### 10.6 Other hooks

1. Tray menu: after "Printer monitor", `pystray.MenuItem(lambda item: tr("MapleJet monitor"),
   lambda icon, item: app.call(app.show_hx_monitor))`; `App.show_hx_monitor()` calls
   `hx_ui.open_monitor(self, self.hx)`.
2. Quitting: `quit_app()` and `_on_close()` (when called from the window's close box with no tray)
   first return if `not hx_ui.can_quit(self, self.hx)`; then `hx_ui.stop_services(self.hx, timeout_s=2.0)`
   runs before `self.destroy()`. The question is asked once per quit.
3. Self-check: `_self_check()` calls `hx_ui.self_check(root)` inside its existing `try`.
4. `i18n.py` gets:

   ```python
   def add_spanish(entries):
       """Add a module's own Spanish texts (each MapleJet module keeps its own ES dict)."""
       for english, spanish in entries.items():
           if ES.get(english, spanish) != spanish or _ENGLISH.get(spanish, english) != english:
               raise ValueError(f"conflicting Spanish for {english!r}")
           ES[english] = spanish
           _ENGLISH[spanish] = english
   ```

   Every `hx_` module with `N_()`/`tr()` texts defines `ES = {...}` near its top and calls
   `i18n.add_spanish(ES)` right after it, so parallel builders never edit the same catalogue. The new
   `stripes.py` texts (e.g. "MapleJet monitor" → "Monitor MapleJet", deploy.md's "Version {version}
   (development key)" → "Versión {version} (clave de desarrollo)") go into `i18n.ES` directly.
5. The printer-password key (deploy.md §1.3, exactly as written there): `import hx_key` in a
   `try`/`except ImportError` right after the `VERSION` import block, setting `HX_KEYED`;
   `check_release_key(version, keyed)` and `version_text()` in the "Versions" section;
   `App._build` and `App._language_changed` use `version_text()` for the version label;
   `_self_check()` calls `check_release_key(VERSION, HX_KEYED)` first and `hx_ui.self_check(root)` last
   (item 3).

## 11. OPEN items (confirm on the test unit)

| Item | Interface | Safe default built now |
|---|---|---|
| Encoder calibration (head distance; a print only shows it within one bag) | `Calibration` record, `CalibrationWindow`, `TestPrintRun(kind="spacing" / "head_distance")`, `head_distance_from_print(sent, measured, length, rough)` picking the multiple of the film length nearest the tape measurement | An encoder machine with `head_distance_mm is None` can't be sent to (`PlanError` "Machine not calibrated") but can be calibrated (send.md §15.2 item 6); the spacing check only reports the measured gap and a whole-mm wheel size, which is corrected on the printer (never by Stripes) and on the machine record; the head distance is worked out from the run that printed the mark (ui.md §13) |
| Which counter counts bags | `Company.bag_counter` = `"PMCNT"`, `"MSCNT"` or `"none"`; `PollStatus.bag_count` | `"PMCNT"`; `"none"` makes test steps timed (`Machine.seconds_per_bag × bags_per_step`, with the 60 s no-print stop replaced by the whole test time plus a margin) |
| Speed units and range | `Machine.print_speed` sent as PRNSPD | cm/min; no default value (an administrator must enter it); accepted range 1–2500; a refused value fails the build step and the read-back check |
| Beacon | none: `BEACON_SUPPORTED = False` | Never queried or switched; a future command would have to be added to `ALLOWED_WRITE_TAGS` deliberately |
| Font list and firmware source | `PrinterClient.fonts()`, `firmware()` | `GET /sfl.xml` and `GET /idn.xml` (§6.4, protocol.md §10.8); a missing font list fails the machine check (red), never passes it. Confirm the files, whether they need the cookies, and that fetching them doesn't disturb printing |
| Item bounds and the print area (BOUND units, the 256 × 160 canvas against the 1024 × 160 preview) | `hx_message.compare` rule 6 (send.md §8.1) | an item whose BOUND runs outside 0..1024 by 0..160 fails the read-back (red); lower the x limit to 256 if the test unit shows the canvas width clips |
| Film print length (film-stop warning) | `Film.print_length_mm` | `None`: no warning possible, shown as "print length unknown" in the Send window |
| Item position units, gaps, escaping of `& < >` and `ñ` | `PrintTextItem.gap`, `Command.block` escaping | protocol.md and send.md choose; the read-back compare catches any mismatch as red |
| Midnight date rollover in TD | `clock_command(local)` | send.md chooses the DTRH/DTRM values; the clock read-back flags a mismatch |
| Reachability probe for new PCs (a PC gets printers only after reaching one) | `HeartbeatInfo.can_poll`, `last_printer_reach`, `PrinterClient.probe()` | storage.md §12.3: a bare TCP connect to port 80 with no request, through `probe()` (never while this PC's gate is busy), at most every 60 s, only to a printer that has no live poller and no lock; confirm it doesn't disturb a printing printer |

## 12. Decisions made in this spec

1. **Module split.** Besides the suggested modules: `hx_core` (clock, bus, cancel, `Msg`; no
   dependencies, so every layer can use them), `hx_message` and `hx_state` (the pure logic split by
   owner spec, so send and state rules can be built in parallel), `hx_win` (every Windows call in one
   place with fallbacks, so the rest tests on Linux), and `hx_ui_common` (shared widgets, so the three
   screen modules never import each other). `hx_model` holds only types, constants and JSON mapping.
2. **Poller threads.** One coordinator plus one worker per polled printer, not a single thread: one PC
   polling all 12 printers needs about 0.9 s of request time per second (≈80 ms × 0.6 requests/s ×
   12, plus a 1.7 s web read per printer per minute), and a dead printer's 2 s timeouts would delay the
   others. Each printer is still asked one request at a time.
3. **Write whitelist.** `PrinterClient` refuses every write not in `ALLOWED_WRITE_TAGS`, which also
   leaves out CLOT and the lot slots (not used) and everything printer-wide. Multi-value queries are
   refused too.
4. **Warm-up read before a write.** After 3 s of quiet a write is preceded by a checked HXUI read, so
   the "first request after a quiet spell" failure never hits a write (writes are never retried).
5. **Injectable gates.** `GateRegistry` is a parameter so a multi-PC simulation in one process tests
   the share lock and the assignment rather than the in-process gate.
6. **In-process lock registry.** `ShareStore.local_locks` makes the local poller step aside for a
   local Send even when the share is down.
7. **Handover rule** (`yielded_to`) so a returning rank-0 poller never overlaps the one that covered
   for it.
8. **JSON keys = field names; one file per record** under `config\hx\` and `templates\`
   (storage.md fixes the layout), so record IDs map straight to file names.
9. **No deletes.** There is no delete API; records are set Not active (`active=False` or status
   `inactive`). IDs are `<prefix>_<10 hex>`.
10. **Only senders send.** Sending, Stop, Resume and Set clocks need the sender group; administrators
    who send are put in both groups. Either group may acknowledge.
11. **One printer per machine.** A printer record belongs to at most one active machine; the admin
    screens refuse a second.
12. **Calibration evidence is its own record** (`Calibration`, keyed by machine), and the value used
    at Send stays on `Machine.head_distance_mm`.
13. **v2 everywhere for the new version**, including the Zebra side: the Zebra config is read from
    `v2\config` (falling back to `ConfigPath`, read-only, until the folder script has run); Zebra
    per-user settings and cache move under `v2` with a one-time copy of `settings.json` and of the
    saved config copy (§10.5); the
    deployment log is written to `v2\status\deployment\` (the folder script creates it with Domain Users
    Modify) and the Deployment status window also reads today's folder, so rollout stays visible.
14. **MapleJet per-user settings in their own file** (`maplejet.json`), so `load_settings()` for Zebra
    is unchanged.
15. **Send-log queue in `%PROGRAMDATA%\Stripes\v2\send-queue`.** Files are never edited or deleted
    by another user (ProgramData's default permissions don't allow it); an uploader adds a
    `<name>.uploaded` marker, and the share side refuses duplicates by file name. Uploading is tried
    when a sender or administrator is signed in; with the plan's permissions only senders can add to
    `logs\hx-sends`, so a refused upload simply stays queued.
16. **Per-module Spanish catalogues** through `i18n.add_spanish`, so parallel builders don't edit one
    dictionary. Non-UI modules never call `tr()`; they produce `Msg`s, and logs stay in English.
17. **Bag text widgets.** `BagText` and `PictureView` are canvases marked `hx_no_translate`, and
    `_retranslate` skips marked widgets as a second guard.
18. **Blue** means a Send or test in progress (the Zebra monitor's "busy" colour).
19. **The Zebra shortcuts** also ignore MapleJet windows, not just the MapleJet tab.
20. **Cancel only during the countdown.** After the first write the Send runs to the end.
21. **Garbage collection on the Tk thread** (`gc.disable()` plus `gc.collect()` every 2 s) because of
    the long-lived worker threads.
22. **Drift by fingerprint.** After a confirmed Send the expected state stores the read-back's
    fingerprint; the poller compares the running message's fingerprint (rendered date text left out),
    plus name and slot.
23. **Origin read back by text.** The origin item is the only text starting "Product of " (no other
    text may contain origin words), so `origin_from_design` finds it in any read-back.
24. **Expected-state writers hold the lock.** Confirm and Leave it take the printer lock briefly and
    write only if `expected.send_id` still matches, so a later Send is never overwritten. `LineStatus`
    is informational; colours come from the machine files.
25. **Identity overrides** through environment variables work only in unfrozen runs.
26. **Test data** lives in `testdata/hx/`; `hx_fake` is never imported by application code and is
    runnable as a script for manual UI work.
27. **`Timings` dataclass** instead of module constants, so tests run the real threads faster.
28. **Machine print speed has no default**, and an encoder machine without a head distance can't be
    sent to: missing values stop a Send rather than guessing.
29. **The folder script seeds `config\hx\company.json`** with the two group SIDs, because the admin
    screens need an administrator SID before anyone can edit anything.
30. **Send logs go through `DataService`** (`append_send_log`, `find_send`), the one object that holds
    both the share and this PC's queue.
31. **Every added status field is listed in §5.8**, with its owner, so the `hx_model` builder has one
    source and `from_json` never drops a field another module relies on.
32. **Two small web GETs** (`/sfl.xml`, `/idn.xml`) are the only additions to the web requests (§6.4),
    because the plan requires the font list and no Autopilot query returns it. Listed in §12.1.
33. **Time-bounded holding.** Lock holders pass each call's worst case to `PrinterLock.check`, and
    `load_slot`, `fonts`, `firmware`, `query`, `read` and `command` take a deadline that also bounds
    the wait for this PC's gate, so no request can outlive a lock or a `poll.json` that another PC is
    about to judge stale (§7.3 rule 8). The poller never waits on the time server (`TimeSource.cached()`
    and the `hx-time` thread).
34. **The UI makes the `action_id`** of a Stop, Resume or Set clocks and passes it in, so its thread
    name, its `WorkFailed` ref and its event filter agree from the start.

### 12.1 Plan deviations for the owner to sign off

These are built as described (the safe default) but differ from the plan's wording. Each needs the
owner's yes before release; the README of this spec folder lists them with the OPEN items.

1. **Font list and firmware by web GET** (§6.4): the plan says the Load "is the only web request
   Stripes makes"; Stripes also fetches `/sfl.xml` and `/idn.xml`. The plan's own font-list check needs
   one of them.
2. **Partial share outage** (§7.3 rule 7): one PC that loses the share while others keep it may poll
   standalone alongside them, and their Sends then carry no `no_lock` warning. Standalone checks start
   only after `lock_stale_s` of continuous outage, which removes the overlap for short outages.
3. **A Send made while the share is down is recorded only by the PC that made it, in the same
   session** (send.md §14.1). If that PC restarts before the share comes back, the machine shows red
   `drift` until the next Send instead of amber.
4. **Named residual risks of the printer lock** (§7.3 rule 8, storage.md §11.8), waiting for the
   owner: a PC that freezes in the instant before a request leaves can send that one request late;
   leases assume `time.monotonic()` counts sleep; and the plant rule that these PCs never sleep,
   hibernate or run as VMs that can be paused (the root README's power settings).
