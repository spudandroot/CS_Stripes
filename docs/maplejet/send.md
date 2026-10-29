# MapleJet Hx build spec: send

Part 5 of 7. This file specifies the three modules architecture.md gives to send.md: `hx_message`
(pure: origin wording, the text builder, validation and plan merge, print position and delay, message
names and content codes, the commands of a message, the read-back compare and fingerprint, the clock
command, test steps and calibration maths), `hx_time` (SNTP, local time, daylight-saving detection) and
`hx_send` (the Send pipeline, the sender's confirmation, Leave it, Stop, Resume, Set clocks, the test
print and calibration runs). It also fixes what senders and testers write into `ExpectedState`,
`LineStatus` and the logs. Names, types, layers and threads come from architecture.md and are not
changed here; §1 lists the names this file adds inside its own modules.

Contents: 0 Scope and conventions · 1 Interfaces added here · 2 Bag text and origin wording ·
3 The text builder · 4 Validation and plan merge · 5 Print position and delay · 6 Building a message ·
7 Message names and content codes · 8 Read-back: compare, dates, fingerprint · 9 Time and the printer
clock · 10 The Send pipeline · 11 The sender's confirmation · 12 When a step fails · 13 Something's
wrong, Stop, Resume and Set clocks · 14 Expected state and line status writes · 15 Test print ·
16 Calibration of encoder machines · 17 Logs · 18 Events · 19 Messages · 20 Tests · 21 OPEN items ·
22 Decisions made in this spec

## 0. Scope and conventions

1. Authority: the plan, then architecture.md, then protocol.md and storage.md for their modules, then
   this file for `hx_message`, `hx_time` and `hx_send`. Where this file uses another module it uses only
   the names those files define (`PrinterClient`, `ShareStore`, `PrinterLock`, `DataService`,
   `check_poller_paused`, `clock_from`, ...).
2. Examples follow the public-repo rule and use the `FakeWorld` seed (protocol.md §16.2): host
   192.0.2.10, UID 1109 with password 123321, `time.example.local` with fallback 192.0.2.123,
   "Example Co", "Example Co 2lb Onion", "Line 1" (code L1), "Line 1 Bagger 1" (code B1), film EXCO1
   (Rev C, length 610 mm, position 150 mm, delay 480 ms, font 2, size 4), print text "Packed on +
   origin" (code PKO, revision 1), `EXAMPLE\jsmith`, PCs `FLOOR01` and `FLOOR02`. Example times are on
   29 Oct 2026, Eastern daylight time (UTC−4).
3. **Bag text** is anything that prints, and is English only. It is never passed through `tr()`. Text
   a sender typed (free text, lot) is never put into a `Msg` value. Names from Stripes' own lists
   (record names, country names, the origin words themselves) may be `Msg` values, the same way machine
   names are.
4. `hx_message` is pure: no files, network, threads, Windows or Tk, and no clock. Functions that need
   "today" take it from their arguments (`SendRequest.created_at`, a `datetime` passed in).
5. `hx_time` does network work (SNTP over UDP) and is called from worker threads only. `hx_send` runs on
   worker threads only (architecture §6.7, §7.1).
6. Every printer request goes through `PrinterClient`; every write is one of `ALLOWED_WRITE_TAGS`; every
   share access goes through `ShareStore` or `DataService`. `hx_send` never calls `SETPPARAM` or any
   printer-wide command, and never writes a slot other than 9 or 10.
7. All waits use the injected `Clock` and `Timings` (architecture §5.1). Where this file needs a wait
   that isn't a `Timings` field, it derives it from one, so `TIMINGS.scaled(f)` scales it too:

   | Wait | Value | Default |
   |---|---|---|
   | Poller handshake check interval | `timings.lock_refresh_s / 20` | 0.25 s |
   | Poller handshake limit | `2 × timings.stale_s` | 30 s |
   | Test print counter reading interval | `timings.second_reading_s` | 1 s |
   | Timed test margin | `timings.test_no_print_s` | 60 s |

8. Packaging doesn't change anything here. The output stays the single-file `Stripes.exe` built by
   PyInstaller, signed in CI and deployed as today (architecture §0.6). No MSI and no installer. Nothing
   in these modules adds a dependency: `hx_message` and `hx_time` use the standard library only
   (`hashlib.sha256` through CPython's built-in `_sha2`, `unicodedata`, `socket`, `struct`, `datetime`,
   `math`, `re`, `json`, `secrets`); `hx_send` adds `threading`, `base64` and `traceback`.

## 1. Interfaces added here

Names architecture.md §6.1, §6.5 and §6.7 already define aren't repeated, except where a signature is
explained.

### 1.1 hx_message

```python
# Constants
ORIGIN_PREFIX = "Product of "                     # the origin item always starts with this (plan)
ORIGIN_WORDS = ("Product of", "Grown in", "Origin", "Made in", "Hecho en")   # architecture, exactly
BAG_TEXT_REFUSED = "<>"                           # never sent (OPEN: escaping, §2.1)
TXT_MAX = 250                                     # CTEXT TXT (manual)
AFFIX_MAX = 100                                   # CTDAT PREF and POSF (manual)
PREPRINTED_ORIGIN_MAX = 40
COUNTRY_NAME_MAX = 30
MAX_ITEMS = 99                                    # OBJIDs 10001..10099 (a technical limit only)
FIRST_OBJID = 10001
GAP_MAX = 50                                      # PrintTextItem.gap, canvas units (OPEN)
OFFSET_MAX = {"days": 999, "months": 999, "years": 99}   # CTDAT DAY, MON, YEAR (manual)
FILM_LENGTH_MAX_MM = 5000.0
HEAD_DISTANCE_MAX_MM = 20000.0
SPEED_RANGE = range(1, 2501)                      # PRNSPD as protocol.md allows it (OPEN: units)
CODE_PATTERN = r"[A-Z0-9]{1,3}"                   # line, machine and print-text codes (fullmatch)
FILM_CODE_PATTERN = r"[A-Z0-9]{1,6}"              # FILM_CODE_MAX stays 6
TEST_CODE = "TST"                                 # the print-text code in test and calibration names
RESERVED_CODES = (TEST_CODE,)                     # print texts can't use it
CONTENT_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"   # Crockford base 32
CHECK_LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ"        # 24 letters: no I, no O
SLOT_CHARS = {9: "9", 10: "0"}
NAME_PATTERN = r"([A-Z0-9]{1,6})-([A-Z0-9]{1,3})([0-9]{2})-([0-9A-HJKMNP-TV-Z]{4})-([A-HJ-NP-Z])([90])"
PRINTER_DEFAULT_SETTINGS: tuple[tuple[str, str], ...]   # §6.1; equal to hx_fake.FAKE_DEFAULT_SETTINGS
RESOLUTION_NAMES = {0: "UltraFine", 1: "Fine", 2: "Normal", 3: "Draft"}   # OPEN: spellings of 1..3
IGNORED_SETTINGS = ("RESO",)
ITEM_ZERO_FIELDS = ("ROTAT", "WCOEFF", "GROUP", "COLR", "RP")
DATE_ZERO_FIELDS = ("HOUR", "MIN", "SEC", "DWEEK")
BOOKKEEPING_FIELDS = ("BOUND", "LANG", "AMPM", "HORFLG", "OBJTYP")
ROLLOVER = (0, 0)                                 # TD DTRH, DTRM: the date changes at midnight (OPEN)
CLOCK_SET_TOLERANCE_S = 90.0                      # printer clock vs expected, right after a TD
DATE_SLACK_S = 120.0                              # rendered dates accepted around the read time
MAX_TEST_STEPS = 100
MAX_FONT_SAMPLES = 6
TEST_SAMPLE_GAP = 8                               # canvas units between font samples (OPEN)
SPACING_TOLERANCE_MM = 2.0                        # calibration spacing check (OPEN)
AMBER_NOTES = ("pc_time", "no_lock", "local_copy")
NOTE_TEXTS: dict[str, Msg]                        # note -> what it means (§19); also used by hx_state
STEP_TEXTS: dict[str, Msg]                        # STEP_* and action steps -> a short name (§19)

# Functions
def origin_tokens(text: str) -> tuple[list[str], list[str], str]           # §2.3
def bag_text_problems(text: str, where: Msg) -> list[Msg]                  # §2.1
def origin_change_text(old_origin_line: str, new_origin_line: str) -> str  # §2.5; bag text
def validate_line(line: Line, data: HxData) -> list[Msg]                   # §4.1
def validate_company(company: Company) -> list[Msg]                        # §4.1
def origin_conflicts(data: HxData, company: Company) -> list[Msg]          # §4.1
def render_date(frmt: str, day: date, *, add_days: int = 0, add_months: int = 0,
                add_years: int = 0) -> str                                  # §3.2
def design_parts(plan: MachinePlan) -> tuple[list[tuple[str, list[tuple[str, str]]]], list[tuple[str, str]]]
    # §6.3: the item blocks and the CPPARM children, before naming; values unescaped strings
def expected_settings(settings: PrintSettings) -> dict[str, str]           # §6.1
def canonical_design(items, settings) -> bytes                             # §7.1
def check_letter(text: str) -> str                                         # §7.4
def date_differences(sent: BuiltMessage, readback: MessageDesign, read_local: datetime, *,
                     slack_s: float = DATE_SLACK_S) -> list[Difference]    # §8.2
def cleared(design: MessageDesign) -> bool                                 # §8.5
def resume_refusal(expected: ExpectedState | None, machine: Machine) -> Msg | None   # §13.3
def resume_mismatch(expected: ExpectedState, design: MessageDesign, machine: Machine) -> Msg | None
def result_colour(confirmed: bool, notes: list[str]) -> str                # §14: GREEN, or AMBER with notes
def test_bag_estimate(plan: TestPlan) -> int                               # §15.2
def spacing_gap_mm(first_mm: float, second_mm: float, film_length_mm: float) -> float   # §16
def wheel_suggestion_mm(wheel_mm: float, sent_gap_mm: float, measured_gap_mm: float) -> float
```

### 1.2 hx_time

```python
SNTP_PORT = 123
NTP_EPOCH_OFFSET = 2_208_988_800                  # seconds from 1900 to 1970
def parse_host(host: str) -> tuple[str, int]      # "time.example.local" -> (.., 123); "127.0.0.1:4123" -> (.., 4123)
def to_local(utc: float, *, offset_fn=local_utc_offset) -> datetime   # naive local time of utc
def recent_dst_change(utc: float, *, within_s: float = 86_400.0,
                      offset_fn=local_utc_offset) -> tuple[float, float] | None   # §9.3 item 5

class TimeSource:                                 # additions to architecture §6.5
    timeout_s: float = 2.0                        # per host
    def now_from(self, reading: TimeReading) -> TimeReading: ...
        # the same source, server and offset_s, at clock.time() now; no request

class DstWatcher:                                 # additions
    offset_s: float                               # this PC's UTC offset at the last call
    previous_s: float | None                      # the offset before the last change, None until one
```

### 1.3 hx_send

```python
ACTION_STOP = "stop"                              # action steps (MachineResult.step of an ActionResult)
ACTION_RESUME = "resume"
LOG_TYPES = {"send": "hx-sends", "send_switch": "hx-sends", "send_confirm": "hx-sends",
             "send_leave": "hx-sends", "stop": "hx-sends", "resume": "hx-sends", "clock_set": "hx-sends",
             "test_start": "hx-admin", "test_step": "hx-admin", "test_end": "hx-admin",
             "internal_error": "hx-events"}       # §17

@dataclass(kw_only=True)
class FailureRow:                                 # one machine in the red window (§12.2)
    machine_id: str
    machine_name: str
    line_name: str
    step: str                                     # STEP_*
    step_text: Msg
    msg: Msg | None
    after_switch: bool
    after_switch_text: Msg | None                 # "This machine may be printing the new message, ..."
    previous_name: str                            # what it was last confirmed printing ("" if unknown)
    previous_origin: str                          # bag text
    previous_preview: bytes                       # BMP, b"" if unknown
    differences: list[Difference] = []

def failure_rows(result: SendResult, plan: SendPlan) -> list[FailureRow]
def is_cancelled(result: SendResult) -> bool      # every machine carries the note "cancelled"
def design_to_log(design: MessageDesign) -> dict  # to_json without preview_bmp, plus preview_b64
def design_from_log(d: dict) -> MessageDesign
def result_from_log(entry: dict) -> SendResult    # the "send" entry back into a SendResult (§17.2)
def request_from_log(entry: dict) -> SendRequest  # "Fix and send again" prefill

class SendPipeline:                               # additions to architecture §6.7
    def pending_confirmation(self, send_id: str) -> list[str]: ...
        # blocking: machine ids whose expected state still waits for this Send's confirmation (§11.3)
```

### 1.4 Optional fields added to the dataclasses send.md owns

architecture §5.8 lets send.md add optional fields to `ExpectedState` and `LineStatus`. These are the
only ones; JSON keys are the field names (storage.md §3.1).

```python
@dataclass(kw_only=True)
class ExpectedState:              # added fields
    stop_reason: str = ""         # "user" | "rejected" | "test"; "" while not stopped
    stopped_by: str = ""
    stopped_at: float | None = None

@dataclass(kw_only=True)
class LineStatus:                 # added field
    sender: str = ""              # who made the Send this file describes
```

## 2. Bag text and origin wording

### 2.1 Characters allowed in bag text

`bag_text_problems(text, where)` checks one piece of bag text: the text before or after an item, a
fixed text, a free-text value, a lot, a code, a country name, a pre-printed origin and a test label.

1. Every character must be in U+0020 to U+007E or U+00A0 to U+00FF (printable Latin-1, so `ñ`, `é` and
   `°` are allowed), and must not be `<` or `>`. Tabs, line breaks and every other control character
   are refused.
2. A failure returns `[Msg(N_("{where} contains characters Stripes can't send to a printer: {codes}."),
   {"where": where, "codes": "U+003C, U+003E"})]`, the refused characters as `U+XXXX` codes, each once,
   in order. The characters themselves are never put in the message (rule 0.3).
3. `&` is allowed. It is sent escaped (`&amp;`, protocol.md §4.1) and compared raw, so if the printer
   handles it differently the read-back check fails, which is safe. OPEN: how `&`, `ñ` and `°` print.
4. Spaces are kept exactly in the text before and after an item (they space the items). Free-text
   values and lots have their surrounding spaces stripped before use (§3.3).

### 2.2 The origin phrase

`origin_phrase(countries)` builds the only text that may name a country of origin:

| Countries (in the order picked) | Phrase |
|---|---|
| `["USA"]` | `Product of USA` |
| `["USA", "Canada"]` | `Product of USA and Canada` |
| `["USA", "Canada", "Mexico"]` | `Product of USA, Canada, and Mexico` |
| four or more | `Product of A, B, C, and D` (a comma after every name but the last, then "and") |

Names are `Country.name` exactly. An empty list raises `ValueError` (validation comes first). The
phrase always starts with `ORIGIN_PREFIX` and has no text before or after it.

### 2.3 Origin words in other text

`origin_words_in(text, company) -> list[str]` returns every origin term found in `text`, ignoring case,
accents and punctuation; `[]` means clean.

The terms are `ORIGIN_WORDS`, then for each `Country` in `company.countries` (active or not) its name and
then its aliases. A country that is no longer offered is still an origin word.

Normalisation, `origin_tokens(text) -> (spaced, joined, compact)`:

1. `t = unicodedata.normalize("NFKD", text)`, drop every character with `unicodedata.combining(c)`, then
   `t = t.casefold()`.
2. `spaced = re.findall(r"[^\W_]+", t)`: words split at any punctuation or space.
3. `joined = re.sub(r"[^\w\s]|_", "", t).split()`: punctuation deleted, then split at spaces.
4. `compact = re.sub(r"[\W_]+", "", t)`: letters and digits only.

A term (normalised the same way into `ps`, `pj`, `pc`) matches the text (`ts`, `tj`, `tc`) when any of
these holds; a term whose `pc` is empty never matches:

| Rule | Test | Catches |
|---|---|---|
| a | `ps` appears as a contiguous run in `ts` | "PRODUCT-OF the farm", "Grown in Florida" |
| b | `pc` equals one of `tj` | "U.S.A." for the term "USA" |
| c | `ps` has 2 or more words and `pc` is a substring of `tc` | "MadeIn", "Productofthefarm" |
| d | `pc` equals the joined letters of a run of 2 or more consecutive one-character words of `ts` | "U S A", "O R I G I N" |

The result lists each matching term once, as configured, in term order. With the `FakeWorld` countries
(USA with aliases US, U.S.A. and United States; Canada with CAN; Mexico with MEX):

| Text | Found |
|---|---|
| `Grown in Florida` | `Grown in` |
| `PRODUCT-OF the farm` | `Product of` |
| `MadeIn` | `Made in` |
| `Hécho en México` | `Hecho en`, `Mexico` |
| `U.S.A. grown` | `USA`, `US`, `U.S.A.` |
| `U S A` | `USA`, `US`, `U.S.A.` |
| `united-states` | `United States` |
| `O R I G I N` | `Origin` |
| `Bring us joy` | `US` (aliases match ignoring case: see decision 6) |
| `Can be recycled` | `CAN` |
| `Packed on `, `Lot `, `Best by `, `Original recipe`, `Origins`, `Canadian`, `Fresh & Crisp` | nothing |

Where the check runs:

| When | What is checked | Result |
|---|---|---|
| Print text saved (`validate_print_text`) | each item's text before and after, each fixed text | refused |
| Line saved (`validate_line`), machine saved (`validate_machine`) | the printed code | refused |
| Company saved (`origin_conflicts`) | with the new country list: every print text (any status), every line and machine code | listed as warnings; those records can't be sent until fixed, because Send re-checks them |
| Send (`merge_plan`) | the chosen print text again, the line and machine codes, every free-text value | refused (`PlanError`) |

Not checked: the origin phrase itself, `Film.preprinted_origin` (it is the origin), free-text labels
(questions to the sender, never printed), record names and test labels (fixed formats, §15.2).

### 2.4 Pre-printed origin

| `Film.origin_preprinted` | Print texts allowed with the film | Countries at Send | `MachinePlan.origin_line` |
|---|---|---|---|
| `True` | only those without an origin item | none (ignored) | `f"{film.preprinted_origin} (pre-printed)"`, e.g. `USA (pre-printed)` |
| `False` | only those with exactly one origin item | one or more, required | the origin phrase |

The pre-printed text is shown at Send, on the monitor and floor board and in the log, and stored as
`ExpectedState.origin_line` and `LineStatus.origin_line`. Nothing about origin is printed then.

### 2.5 Origin change text

`origin_change_text(old, new)` gives the Send window its highlight ("USA → USA and Canada"):

1. Each side loses a leading `ORIGIN_PREFIX` if it has one.
2. Equal (after step 1) returns `""`. An empty `old` returns the new side alone.
3. Otherwise `f"{old} → {new}"`: `("Product of USA", "Product of USA and Canada")` gives
   `USA → USA and Canada`; `("USA (pre-printed)", "Product of USA")` gives `USA (pre-printed) → USA`.

The result is bag text and is shown in a `BagText` widget. `old` is what the machine is running now
(`MachineView.origin_line`); the UI calls it once per machine of the line.

## 3. The text builder

### 3.1 Item kinds

Each `PrintTextItem` becomes exactly one Autopilot object. "Value" is the data the item shows.

| `kind` | Object | Value | Text before and after | Rules |
|---|---|---|---|---|
| `fixed` | CTEXT | `item.text` | inside TXT | `text` not empty |
| `packed_date` | CTDAT | the printer's date | PREF and POSF | `offset` must be 0 |
| `best_by_date` | CTDAT | the date plus the offset | PREF and POSF | `offset` 1 to `OFFSET_MAX[offset_unit]` |
| `lot` | CTEXT | `LineChoice.lot` | inside TXT | §3.3 |
| `origin` | CTEXT | the origin phrase (§2.2) | must both be `""` | at most one per print text |
| `free_text` | CTEXT | `LineChoice.free_text[item.id]`, stripped | inside TXT | §3.3; an optional empty value leaves the whole item out |
| `machine_code` | CTEXT | `Machine.code` | inside TXT | |
| `line_code` | CTEXT | `Line.code` | inside TXT | |

1. A CTEXT item's TXT is `before + value + after`, at most `TXT_MAX` characters. PREF and POSF are not
   sent for CTEXT items.
2. A CTDAT item sends PREF = `before` and POSF = `after` only when they aren't empty, each at most
   `AFFIX_MAX` characters.
3. There are no barcodes or QR codes in the first version: `ITEM_KINDS` has no such kind, and CBARC is
   in protocol.md's `UNUSED_WRITES`. Lot slots aren't used either: a lot is plain CTEXT, never CLOT.
4. The font index and size of every item are the film's (`Film.font`, `Film.size`): one font and size
   for the whole print text (plan).

### 3.2 Date formats and offsets

The builder offers its own choices and translates them into the printer's FRMT codes, where each letter
is a whole field (manual §6.4). The keys of `DATE_FORMATS` are what administrators see:

| Choice (key) | FRMT sent | Prints on 29 Oct 2026 |
|---|---|---|
| `MM/DD/YY` | `M/D/y` | `10/29/26` |
| `MM/DD/YYYY` | `M/D/Y` | `10/29/2026` |
| `DD/MM/YY` | `D/M/y` | `29/10/26` |
| `DD/MM/YYYY` | `D/M/Y` | `29/10/2026` |
| `YYYY-MM-DD` | `Y-M-D` | `2026-10-29` |
| `MM-DD-YY` | `M-D-y` | `10-29-26` |
| `MMDDYY` | `MDy` | `102926` |
| `MON DD YYYY` | `m D Y` | `OCT 29 2026` |
| `DD MON YY` | `D m y` | `29 OCT 26` |
| `MON YYYY` | `m Y` | `OCT 2026` |
| `MM/YY` | `M/y` | `10/26` |

Words ("Packed on ", "Best by ") go in the text before or after, never in FRMT.

`render_date(frmt, day, *, add_days, add_months, add_years)` renders what the printer should print, for
the preview, the one-line bag text and the date check (§8.2):

1. Offsets, in this order (the manual's example: 2022/04/10 with YEAR 1, MON 3, DAY 5 prints
   2023/07/15): add the years; add the months, carrying into the year, with the day cut to the last day
   of the resulting month (31 Jan + 1 month = 28 Feb, or 29 in a leap year); then add the days.
2. Codes: `Y` 4-digit year, `y` 2-digit year, `M` 2-digit month, `m` JAN to DEC, `D` 2-digit day. `/`,
   `-`, space and `.` are copied. Any other character raises `ValueError` (the builder never sends one).
3. OPEN: the printer's own month arithmetic at month ends. The date check at every Send flags any
   difference.

A best-by item sends DAY = offset for days, MON = offset for months, YEAR = offset for years, and 0 in
the other two. A packed-on item sends DAY 0, MON 0 and YEAR 0. All three are always sent.

### 3.3 Lot, free text and codes

1. **Lot.** Only when the print text has a lot item. `lot.strip()` must fullmatch
   `[0-9]{min,max}` with `Company.lot_min_digits` and `lot_max_digits`. When the print text has no lot
   item the lot is ignored and the plan's `lot` is `""`.
2. **Free text.** `value = choice.free_text.get(item.id, "").strip()`. A required item needs a value. The
   value is at most `item.max_len` characters, passes `bag_text_problems`, and must have no origin words
   (checked at Send, plan). `before + value + after` is at most `TXT_MAX`. An optional item left empty
   is left out of the message entirely, and the next item is placed relative to the last item actually
   sent.
3. **Codes.** `Line.code` and `Machine.code` fullmatch `CODE_PATTERN` and have no origin words (so a
   machine can't be coded `US` while US is an alias). `PrintText.code` fullmatches `CODE_PATTERN` and
   isn't in `RESERVED_CODES`. `Film.code` fullmatches `FILM_CODE_PATTERN` and is unique among all films
   (records are never deleted, so a new film version takes a new code: EXCO1, then EXCO2).

### 3.4 Placement

Items are placed relative to each other, so a long value pushes the next item along instead of printing
over it (manual §6.2.2 to §6.2.5). The present items, in order, get OBJIDs from `FIRST_OBJID` up:

```
prev = None; line_start = None
for k, item in enumerate(present_items):
    objid = FIRST_OBJID + k
    if prev is None:                         # the first item: absolute
        POS "0,0", MARG "0,0"; no XRID, YRID, SXAS, SYAS; line_start = objid
    elif item.placement == "right":          # right of the previous item, same top
        XRID prev, SXAS 0 (its X-max); YRID prev, SYAS 1 (its Y-min); MARG f"{gap},0"
    else:                                    # "below": a new line under the previous item,
        XRID line_start, SXAS 1 (X-min);     # lined up with the start of the previous line
        YRID prev, SYAS 0 (Y-max); MARG f"0,{gap}"; line_start = objid
    prev = objid
```

1. Every item in a print text has the same font and size, so the previous item's bottom is the bottom
   of its line, and "below" never overlaps.
2. The first item's `placement` and `gap` are ignored. `gap` is 0 to `GAP_MAX` canvas units (OPEN: the
   units and whether a margin from the canvas edge is needed; the test print shows it).
3. Relative items don't send POS (protocol.md §12.2 doesn't compare it then).

### 3.5 The one-line bag text

`MachinePlan.bag_text` and `BuiltMessage.bag_text` render the message on one line, for windows and logs:

1. Each present item's text: a CTEXT item's TXT; a CTDAT item's `before + render_date(...) + after`, for
   the local date of `SendRequest.created_at` (`time.localtime`). Test plans have no dates.
2. Joined in order: the first item's text; then for each next item, `" / "` before it when it is
   placed below, else `" "` when its gap is above 0, else nothing.
3. Example (the storage.md print text, gap 4 on the lot): `Packed on 10/29/26 / Product of USA and
   Canada Lot 4417`. With the `FakeWorld` print text (gap 0): `Packed on 10/29/26 / Product of USA and
   CanadaLot 4417`.

### 3.6 validate_print_text(pt, company)

Returns every problem, in this order (texts in §19):

1. `name` not empty and at most 60 characters; `code` fullmatches `CODE_PATTERN` and isn't reserved;
   `status` in `FILM_STATUSES`.
2. At least 1 and at most `MAX_ITEMS` items; item ids unique and fullmatching `i[0-9]{1,3}`; every
   `kind` in `ITEM_KINDS`; at most one `origin` item.
3. Per item (`n` is its 1-based position): `placement` in `("right", "below")`; `gap` 0 to `GAP_MAX`;
   `before`, `after` and (for `fixed`) `text` pass `bag_text_problems` and have no origin words; an
   origin item's `before` and `after` are `""`; a `fixed` item's text isn't empty; a date item's
   `date_format` is a `DATE_FORMATS` key and its PREF/POSF fit `AFFIX_MAX`; a `packed_date` has offset 0;
   a `best_by_date` has `offset_unit` in `OFFSET_MAX` and offset 1 to its maximum; a `free_text` item
   has a non-empty `label` and `max_len` 1 to `company.free_text_max`; the longest possible CTEXT TXT
   fits `TXT_MAX` (free text: `len(before) + max_len + len(after)`; lot: with `lot_max_digits`; origin:
   with every active country, in list order; codes: 3 characters).

## 4. Validation and plan merge

### 4.1 Records

These are called by the admin screens before saving (ui.md) and again, for the records used, by
`merge_plan` and `plan_test`. A non-empty list blocks the save, except `origin_conflicts`, which is a
warning.

`validate_company(company)`:

1. Country names not empty, unique ignoring case, at most `COUNTRY_NAME_MAX` characters, passing
   `bag_text_problems`. Every alias not empty and not used by another country, as a name or an alias,
   ignoring case.
2. Ink names not empty and unique; each `cartridge` in `CARTRIDGES` and `colour` in `INK_COLOURS`.
3. `1 <= lot_min_digits <= lot_max_digits <= 20`; `free_text_max` 1 to 100.
4. `standard_fonts`: exactly 6 non-empty names. `bag_counter` in `("PMCNT", "MSCNT", "none")`.

`origin_conflicts(data, company)`: with `company`'s countries, each print text (any status) whose items
now hold origin words gives "Print text {name} now contains words about origin: {words}.", and each line
or machine whose code does gives "The code of {name} now contains words about origin: {words}."
(`words` joined with ", ").

`validate_line(line, data)`: `name` not empty, at most 40 characters; `code` (§3.3); `cartridge` in
`CARTRIDGES`.

`validate_machine(machine, data)`:

1. `name` not empty, at most 60 characters; `code` (§3.3); `line_id` names a line; `printer_id` names a
   printer that no other active machine uses (architecture decision 11); `placement` in `PLACEMENTS`.
2. `print_speed` is None or in `SPEED_RANGE`; `seconds_per_bag` 0.5 to 60.
3. Encoder machines: `head_distance_mm` None or above 0 and at most `HEAD_DISTANCE_MAX_MM` (it may be
   longer than a bag); `encoder_wheel_mm` None or 20 to 150; `encoder_ppr` None or 1 to 1,000,000.
   A missing head distance or speed doesn't block saving; it blocks sending (§4.4).

`validate_film(film, data)`:

1. `product_id` names a product; `code` (§3.3); `status` in `FILM_STATUSES`; `side` in `("front",
   "back")`; `cartridge` in `CARTRIDGES`; `ink_colour` in `INK_COLOURS`.
2. When set: `length_mm` above 0 and at most `FILM_LENGTH_MAX_MM`; `position_mm` from 0 to the length
   (the length must be set too); `delay_ms` 0 to 120,000; `print_length_mm` above 0 and at most the
   length.
3. `font` in `FONT_INDEXES`, `size` in `SIZE_RANGE`, `resolution` in `RESOLUTIONS`, `bold` in
   `BOLD_RANGE`, `width` in `WIDTH_RANGE`.
4. `origin_preprinted`: `preprinted_origin` not empty, at most `PREPRINTED_ORIGIN_MAX`, passing
   `bag_text_problems`.
5. Every id in `print_text_ids` names a print text, and each obeys the origin rule of §2.4.
   `default_print_text_id` is `""` or one of them. An active film has at least one print text and a
   default.
6. Which placement values are required depends on the machine, so it is checked at Send (§4.4): length
   and position for encoder machines, delay for delay machines.

### 4.2 What is offered at Send

`films_for_line(line, product_id, data)`: the films with `product_id`, `status == "active"` and
`cartridge == line.cartridge`, sorted by `(artwork_revision, code)`. No film is pre-selected (plan).

`print_texts_for_film(film, data)`: the print texts in `film.print_text_ids`, in that order, that are
active, obey the film's origin rule (§2.4) and have no `validate_print_text` problems with the current
company. The UI pre-fills `film.default_print_text_id` when it is in the list.

### 4.3 validate_choice(choice, data, identity)

Every problem with one `LineChoice`, in this order (texts in §19; `{line}` is `Line.name`):

1. The line exists and is active.
2. `machine_ids` is not empty; each is an active machine of that line (`data.line_machines`); no
   repeats.
3. The product exists and is active; the film is in `films_for_line(line, product_id, data)`; the print
   text is in `print_texts_for_film(film, data)`.
4. The lot (§3.3), when the print text has a lot item.
5. Countries, unless the film is pre-printed: at least one; each a `Country.name` of an active country;
   no repeats.
6. Free text (§3.3), for each free-text item of the print text.
7. The print text again with `validate_print_text`, the line and every machine with `validate_line` and
   `validate_machine`, the film with `validate_film`.
8. Per machine: its printer exists, is active and has a token (`Printer.token != ""`); `print_speed` is
   set; encoder machines: `head_distance_mm` is set ("isn't calibrated") and the film has `length_mm`
   and `position_mm`; delay machines: the film has `delay_ms`; the position or delay works out
   (`explain_position` doesn't raise).

`identity` is used only by `merge_plan`'s sender check; `validate_choice` accepts it so the Send window
can call it per line while the sender fills it in.

### 4.4 merge_plan(request, data, identity)

1. `identity.is_sender(data.company)` is false: raise `NotAllowed(Msg(N_("Only Stripes MapleJet Senders
   can send.")))`.
2. `request.lines` is empty: problem "Choose at least one line." A line id twice, a machine id twice
   across lines, or two machines with the same printer: a problem each.
3. `validate_choice` for each line, problems appended in order.
4. Any problem: raise `PlanError(problems[0], problems=problems)`. Nothing has been written anywhere.
5. Otherwise build one `MachinePlan` per machine, lines in request order and machines in
   `machine_ids` order:

   | Field | Value |
   |---|---|
   | `machine`, `line`, `printer`, `product`, `film`, `print_text` | `copy.deepcopy` of the records |
   | `adjustment` | `data.adjustment(machine_id, film_id)` (a copy) or None |
   | `lot` | the stripped lot, or `""` without a lot item |
   | `countries` | the chosen names, or `[]` for a pre-printed film |
   | `free_text` | item id → stripped value, only for free-text items present |
   | `origin_line` | §2.4 |
   | `settings` | §6.1 |
   | `position` | `explain_position(machine, film, adjustment)` |
   | `warnings` | the film-stop warning when there is one (§5.4); for an encoder machine whose film has no `print_length_mm`, "Print length unknown: ..." |
   | `bag_text` | §3.5 |
   | `test_step` | None |

6. Return `SendPlan(send_id=request.send_id, request=request, machines=..., from_local_copy=(data.source
   == "local"), data_as_of=data.reached_at)`.

`merge_plan` is deterministic: the same request and data give an equal plan. The pipeline relies on
that to detect templates that changed after the sender looked (§10.2).

### 4.5 Try again: SendPlan.restricted

`plan.restricted(machine_ids)` (architecture §5.6) keeps the request's choices for those machines only,
drops lines left without machines, and takes a new `send_id` (`new_id("sd")`), also written into the
request's `send_id`. `created_at` and everything else stay as they were. Try again runs it through
`SendPipeline.run` like any Send (§12.3).

## 5. Print position and delay

### 5.1 Encoder machines

On encoder machines the bagger's cycle signal triggers each print with the film at its registered stop;
the encoder then measures the film's movement, so the print lands by distance and machine speed doesn't
matter. With the encoder on, the printer takes the print position in mm after the trigger (DLYMM), and
the count restarts at every trigger, so a Send never resets it.

```
sent = (print position − head distance + adjustment) mod film length
```

`print_position_mm(position_mm, head_distance_mm, film_length_mm, adjustment_mm=0.0) -> int`:

1. `film_length_mm <= 0` raises `ValueError`.
2. `x = (position_mm - head_distance_mm + adjustment_mm) % film_length_mm` (Python's `%`, so `x` is in
   `[0, film_length_mm)` even for a negative left side).
3. `sent = math.floor(x + 0.5)` (half up); if `sent >= film_length_mm`, `sent = 0` (x rounded up to the
   length is the same spot as 0).

The head distance is measured along the film path from the print head back to where a bag's bottom edge
sits when the film stops, and may be longer than one bag: the modulo takes care of it. When the trigger
fires, the head sits over a different part of the bag for each film length; if the print position has
already passed the head, the code goes on the same spot of the next bag. That is the wrap.

| Position | Head | Length | Adjustment | Sent | Wrapped |
|---|---|---|---|---|---|
| 150 | 412 | 610 | 0 | 348 (the plan's example) | yes |
| 150 | 412 | 610 | −3 | 345 | yes |
| 500 | 412 | 610 | 0 | 88 | no |
| 150 | 1300 | 610 | 0 | 70 | yes |
| 412 | 412 | 610 | 0 | 0 | no |
| 150.5 | 412 | 610 | 0 | 349 | yes |

`wrapped` is true when `position − head + adjustment` is below 0 or at least the length.

### 5.2 Delay machines

There is no encoder and no maths: the printer waits the film's delay plus the machine + film delay
adjustment after the trigger, sent in microseconds (DELAY). There is no wrap and no calibration.

`delay_us(delay_ms, adjustment_ms=0.0) -> int`: `us = math.floor((delay_ms + adjustment_ms) * 1000 +
0.5)`. Below 0 or above `DELAY_US_MAX` raises `PlanError(Msg(N_("The delay works out at {ms} ms, outside
0 to 120000 ms.")), {"ms": ...})`. `(480, 20)` gives 500000; `(480, 0)` gives 480000; `(10, -20)` raises.

### 5.3 explain_position(machine, film, adjustment, *, override=None, test=False)

Returns the `PositionWorking` shown in the Send window and stored in the log. `override` replaces the
film's value (a test step's); `test=True` ignores the adjustment (plan: "Ignored during a test print").
Missing values raise `PlanError` with the §4.3 messages.

| Field | Encoder machine | Delay machine |
|---|---|---|
| `placement` | `"encoder"` | `"delay"` |
| `film_value` | `override` or `film.position_mm` | `override` or `film.delay_ms` |
| `adjustment` | `0.0` if `test` or no record, else `adjustment.position_mm` | `0.0` if `test` or no record, else `adjustment.delay_ms` |
| `head_distance_mm` | `machine.head_distance_mm` | None |
| `film_length_mm` | `film.length_mm` | `film.length_mm` (may be None) |
| `sent` | `print_position_mm(...)`, mm | `delay_us(...)`, µs |
| `wrapped` | §5.1 | False |
| `warning` | `film_stop_warning(sent, film.print_length_mm, film.length_mm)` | None |
| `text` | `(150 - 412 + 0) mod 610 = 348 mm` | `480 + 20 = 500 ms = 500000 µs` |

Text rules: numbers through `_num(x)`, which is `f"{x:.1f}"` without a trailing `.0` (150.0 → `150`,
152.5 → `152.5`, −3.0 → `-3`); the adjustment is written `+ a` or `- |a|` (`(150 - 412 - 3) mod 610 =
345 mm`); in a test the text ends with ` (test print: adjustment ignored)`.

### 5.4 The film-stop warning

The printer ignores the next trigger while it is still printing, so if the code would still be printing
when the film stops at the end of a pull, the next bag goes out blank. On encoder machines the pull
after a trigger is one film length.

`film_stop_warning(sent_mm, print_length_mm, film_length_mm)`:

1. `print_length_mm is None`: None. The plan leaves the print length open; `merge_plan` then adds the
   "Print length unknown" warning instead (§4.4), shown in the Send window.
2. `sent_mm + print_length_mm > film_length_mm`: `Msg(N_("The print would still be running when the
   film stops ({end} of {length} mm), so the next bag would go out blank."), {"end": _num(sent +
   print_length), "length": _num(film_length)})`.
3. Otherwise None.

The warning is shown in the Send window before the countdown, logged, and noted as `film_stop` in the
`MachineResult`. It doesn't stop the Send and doesn't change the colour. Delay machines get no check:
the film speed isn't known. OPEN: how the print length is measured, and whether it differs with the
encoder on (plan, "Still to test").

## 6. Building a message

### 6.1 Print settings

`merge_plan` (and `test_step_plan`) fill `MachinePlan.settings`:

| `PrintSettings` field | CPPARM child | Value |
|---|---|---|
| `encoder` | ENCST | 1 on encoder machines, 0 on delay machines |
| `position_mm` | DLYMM | `position.sent`, encoder machines only (0 otherwise, not sent) |
| `delay_us` | DELAY | `position.sent`, delay machines only (0 otherwise, not sent) |
| `speed` | PRNSPD | `Machine.print_speed` (required; OPEN: units and range) |
| `rotate`, `mirror` | ROTAT, MIRR | `Machine.rotate`, `Machine.mirror` |
| `resolution`, `bold`, `width` | RESMOD, BOLD, WDTH | the film's, or the test step's |
| `quantity` | QUANT | 1 |
| `invert` | REVRS | 0 |
| `auto_repeat` | AUTREP | 0 |

The CPPARM block always sends these 11 children in this order: ENCST, then DLYMM or DELAY, then PRNSPD,
ROTAT, MIRR, RESMOD, BOLD, WDTH, QUANT, REVRS, AUTREP. Booleans are sent as 0 and 1. Stripes never sends
SETPPARAM ("set current print settings"): it would change the message printing now.

`PRINTER_DEFAULT_SETTINGS` is what the printer gives a message for everything not sent (seen on the
slot 9 capture, protocol.md §12.3), in the printer's order:

```
ROTAT 0, BOLD 1, WDTH 1, QUANT 1, DELAY 0, QDELAY 0, AUTREP 0, REVRS 0, DRAFT 0, MIRR 0, SLANT 0,
ANG 30, SWDLY 0, SWSPD 250, WPDLY 0, PRNSPD 1000, ENCST 0, RESMOD 0, DLYMM 0, QDLYMM 0, ENCO 0,
RESO 640, RESNAM UltraFine, ENCSCL 100
```

`expected_settings(settings)` is `dict(PRINTER_DEFAULT_SETTINGS)`, with every child sent replacing its
default, `RESNAM = RESOLUTION_NAMES[resolution]`, and `RESO` removed (not compared): 23 keys. So QDELAY,
QDLYMM, ENCSCL and whichever of DLYMM and DELAY wasn't sent must read back as their defaults, and DRAFT,
SLANT, ANG, SWDLY, SWSPD, WPDLY and ENCO, which Autopilot can't set, must match the printer's default or
the check fails (plan).

### 6.2 The children of each command

Each item is one `Command.block(tag, children)`; children are sent in this order, values as strings
(`str()` of an int), escaping done by `Command.block`:

| Object | Children, in order |
|---|---|
| CTEXT | OBJID; POS (first item only) or XRID, YRID, SXAS, SYAS (relative items); MARG; FONTIND; SIZE; TXT |
| CTDAT | OBJID; POS or XRID, YRID, SXAS, SYAS; MARG; FONTIND; SIZE; PREF (if not empty); POSF (if not empty); FRMT; DAY; MON; YEAR |
| CPPARM | §6.1 |
| TD | §9.4 |

### 6.3 build_message(plan, slot)

1. `slot` must be in `SLOTS`, else `ValueError`.
2. `items, settings = design_parts(plan)`: the item blocks of §3.1 to §3.4 (or, for a test plan, the
   label and font samples of §15.3) and the CPPARM children of §6.1. A value out of range raises
   `ValueError`: validation comes first, and protocol.md's `check_write` would refuse it anyway.
3. `content = content_code(plan)` (§7.2), `name = message_name(plan, slot)` (§7.3).
4. `commands = [Command.set("CRTMSG", name)] + [Command.block(tag, children) for each item] +
   [Command.block("CPPARM", settings)] + [Command.set("SAVEMSG", slot)]`.
5. `expected_items = [DesignItem(tag=tag, pairs=children)]`, values unescaped, exactly as sent.
6. Return `BuiltMessage(slot, name, content_code=content, commands, expected_items,
   expected_settings=expected_settings(plan.settings), origin_line=plan.origin_line,
   bag_text=plan.bag_text)`.

`build_commands(plan, slot)` is `build_message(plan, slot).commands`. Both are deterministic.

### 6.4 Golden example

The `FakeWorld` delay machine, the seeded film and print text, countries USA then Canada, lot 4417,
slot 9. The commands' `xml`, one per line (each is its own request):

```
<CRTMSG>EXCO1-PKO01-MN6D-L9</CRTMSG>
<CTDAT><OBJID>10001</OBJID><POS>0,0</POS><MARG>0,0</MARG><FONTIND>2</FONTIND><SIZE>4</SIZE><PREF>Packed on </PREF><FRMT>M/D/y</FRMT><DAY>0</DAY><MON>0</MON><YEAR>0</YEAR></CTDAT>
<CTEXT><OBJID>10002</OBJID><XRID>10001</XRID><YRID>10001</YRID><SXAS>1</SXAS><SYAS>0</SYAS><MARG>0,0</MARG><FONTIND>2</FONTIND><SIZE>4</SIZE><TXT>Product of USA and Canada</TXT></CTEXT>
<CTEXT><OBJID>10003</OBJID><XRID>10002</XRID><YRID>10002</YRID><SXAS>0</SXAS><SYAS>1</SYAS><MARG>0,0</MARG><FONTIND>2</FONTIND><SIZE>4</SIZE><TXT>Lot 4417</TXT></CTEXT>
<CPPARM><ENCST>0</ENCST><DELAY>480000</DELAY><PRNSPD>2400</PRNSPD><ROTAT>0</ROTAT><MIRR>0</MIRR><RESMOD>0</RESMOD><BOLD>1</BOLD><WDTH>1</WDTH><QUANT>1</QUANT><REVRS>0</REVRS><AUTREP>0</AUTREP></CPPARM>
<SAVEMSG>9</SAVEMSG>
```

The same on the `FakeWorld` encoder machine (head distance 412 mm): the name is
`EXCO1-PKO01-5E11-W9`, the items are identical, and the CPPARM starts
`<CPPARM><ENCST>1</ENCST><DLYMM>348</DLYMM><PRNSPD>2400</PRNSPD>` and continues as above.

### 6.5 Empty messages

`empty_message_commands(slot)` returns `[Command.set("CRTMSG", ""), Command.set("SAVEMSG", slot)]`
(`<CRTMSG></CRTMSG>`, then `<SAVEMSG>9</SAVEMSG>`): a design with no name and no items. The printer has
no delete command, so this is how the end of a test overwrites its slots (§15.5). It is sent only while
printing has been confirmed off. OPEN: that the printer accepts SAVEMSG of a design without items.

### 6.6 The idle slot

`idle_slot(printing, printing_slot)` returns the slot of `SLOTS` that isn't `printing_slot` when
`printing_slot` is 9 or 10, and `DEFAULT_IDLE_SLOT` (9) otherwise. The answer doesn't depend on
`printing`: Stripes alternates even when printing is off, so the selected slot is never rewritten
during a Send and a Print On pressed at the printer mid-build never prints a half-built message. This
is stricter than the plan's rule ("never writes to the slot that's printing while printing is on"),
which it implies. The only writes to the selected slot are the end-of-test empty messages, sent after
printing is confirmed off.

`printing_slot` always comes from the printer itself: the `MSNO` of `load_slot(0)` at the machine check
(§10.5), never from memory or a status file.

## 7. Message names and content codes

### 7.1 The canonical design

`canonical_design(items, settings) -> bytes` is the exact byte string the content code is computed from:

```python
obj = {"v": 1,
       "items": [[tag, [[child, value] for child, value in children]] for tag, children in items],
       "settings": [[child, value] for child, value in settings]}
json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
```

`items` and `settings` are `design_parts(plan)`: values are the unescaped strings sent, in the order
sent. The name (CRTMSG) and the slot (SAVEMSG) are not in it. So the canonical design changes with
anything that prints: any text, the lot, the countries and their order, free text, codes, fonts, sizes,
placement, gaps and every print setting. It doesn't change from day to day, because dates are sent as
FRMT and offsets and rendered by the printer.

For the golden example the canonical design is 679 bytes, starting
`{"v":1,"items":[["CTDAT",[["OBJID","10001"],["POS","0,0"],` and with sha256
`a54cde6dbf91adca88911db3bf74c1bfa9f6ee5516f245e8bd71c13f50d3166b`.

### 7.2 The content code

`content_code(plan)`: `h = hashlib.sha256(canonical_design(*design_parts(plan))).digest()`; `n =
int.from_bytes(h[:3], "big") >> 4` (the first 20 bits); the code is
`"".join(CONTENT_ALPHABET[(n >> s) & 31] for s in (15, 10, 5, 0))`: 4 characters of Crockford base 32.

It identifies the content for people and logs and makes every different message's name differ (a
collision is about 1 in a million). It proves nothing by itself: the field-by-field compare (§8.1) does.

### 7.3 The name

`message_name(plan, slot)`:

```
{film code}-{print text code}{revision:02d}-{content code}-{check letter}{slot char}
```

| Part | Value | Length |
|---|---|---|
| film code | `Film.code` | 1 to 6 |
| print text code | `PrintText.code`; `TST` (`TEST_CODE`) in a test print or calibration run | 1 to 3 |
| revision | `PrintText.revision % 100`, two digits; in a test, the step index `% 100` | 2 |
| content code | §7.2 | 4 |
| check letter | §7.4 | 1 |
| slot char | `SLOT_CHARS[slot]`: `9` for slot 9, `0` for slot 10 | 1 |

With the three hyphens, a name is 13 to 20 characters, never more than `MESSAGE_NAME_MAX`. The full
print-text revision is in the log; the name carries its last two digits only.

### 7.4 The check letter

`check_letter(text)` is `CHECK_LETTERS[int.from_bytes(hashlib.sha256(text.encode("ascii")).digest()[:4],
"big") % 24]`, where `text` is the whole name without its check letter: `f"{film}-{code}{rev:02d}-{cc}-"
+ slot_char`. A reply that only looks like a name (a stray `1`, `None`, another tool's name) fails the
format or the letter: a random change passes with a chance of 1 in 24.

### 7.5 parse_message_name(name)

1. `re.fullmatch(NAME_PATTERN, name)`; no match returns None.
2. Recompute the check letter from the groups; a different letter returns None.
3. Return `MessageName(film_code=g1, print_text_code=g2, print_text_revision=int(g3),
   content_code=g4, slot=9 if g6 == "9" else 10)`.

`hx_state` uses it to recognise Stripes' own messages and their slot from MSGNAM (states.md).

### 7.6 Golden values

| Case | Content code | Slot 9 name | Slot 10 name |
|---|---|---|---|
| Golden example, delay | `MN6D` | `EXCO1-PKO01-MN6D-L9` | `EXCO1-PKO01-MN6D-U0` |
| Golden example, encoder (DLYMM 348) | `5E11` | `EXCO1-PKO01-5E11-W9` | `EXCO1-PKO01-5E11-D0` |
| Delay, lot 4418 instead of 4417 | `RVFJ` | | |
| Longest: film `ABCDEF`, code `XYZ`, revision 123, content `ZZZZ` | | | `ABCDEF-XYZ23-ZZZZ-V0` (20 characters) |

`parse_message_name("EXCO1-PKO01-MN6D-L9")` is `MessageName("EXCO1", "PKO", 1, "MN6D", 9)`;
`"EXCO1-PKO01-MN6D-M9"`, `"STRIPES TEST"`, `"1"` and `""` give None.

## 8. Read-back: compare, dates, fingerprint

### 8.1 compare(sent, readback)

Implements protocol.md §12 for messages Stripes built. Every value is compared as the raw string the
parser returned: no trimming and no entity decoding. It returns every difference, `[]` when it matches.

1. `readback.empty`: one `Difference("message", "empty", "a message", "an empty slot")`, and stop.
2. `readback.slot != sent.slot`: `Difference("slot", "MSNO", ...)`. `readback.name != sent.name`:
   `Difference("name", "MSGNAM", ...)`. `width` or `height` present and not 256 or 160:
   `Difference("message", "W" or "H", ...)`.
3. Item count differs: `Difference("items", "count", ...)`. Then item by item up to the shorter list,
   `where = f"item {n} ({tag})"`: a different tag gives `Difference(where, "tag", ...)` and that item is
   skipped; otherwise the item rules below.
4. Print settings, `where = "print settings"`: every key of `sent.expected_settings` must be in
   `readback.settings` (else read `"(missing)"`) with the same value; every other key of
   `readback.settings`, except `IGNORED_SETTINGS`, is a difference with sent `"(not expected)"`.
5. Picture: when any expected item has a non-empty TXT or PREF and `readback.black_pixels == 0`:
   `Difference("picture", "BPIX", "more than 0", "0")`. The picture itself is judged by the sender.

Item rules. `sent` is the expected item's pairs as a dict (Stripes never repeats a child); every field
of the read-back item is checked, and every occurrence of a repeated field must pass:

| Read-back field | Must be |
|---|---|
| `BOUND`, `LANG`, `AMPM`, `HORFLG`, `OBJTYP` | anything (bookkeeping) |
| `TXT` of a CTDAT | anything here; §8.2 checks it |
| `SIZE` | `str(int(sent["SIZE"]) - 1)`: read back one lower |
| `POS`, `MARG`, `OBJID` | as sent when sent; anything when not sent |
| `XRID`, `YRID`, `SXAS`, `SYAS` | as sent; `"0"` when not sent |
| `DAY`, `MON`, `YEAR` (CTDAT) | as sent; `"0"` when not sent |
| `TXT` (CTEXT), `PREF`, `POSF`, `FONTIND`, `FRMT` | as sent; `""` when not sent |
| `ROTAT`, `WCOEFF`, `GROUP`, `COLR`, `RP` | `"0"` |
| `HOUR`, `MIN`, `SEC`, `DWEEK` (CTDAT) | `"0"` |
| anything else | a difference, sent `"(not expected)"`: an unknown field means the format changed |

And every field sent must be present in the read-back (else read `"(missing)"`).

`Difference.sent` and `.read` may hold bag text. They are shown in `BagText` widgets and stored in the
log, never put in a `Msg`.

### 8.2 date_differences(sent, readback, read_local, *, slack_s)

A CTDAT's TXT is the date as the printer rendered it when the Load ran. Checking it proves the printer's
clock, its date rollover, the format and the offset all give the date Stripes showed the sender.

1. For each position `n` where both the expected and the read-back item are CTDAT: `frmt`, `DAY`, `MON`
   and `YEAR` come from the expected item; `got = readback_item.value("TXT", "")`.
2. The accepted texts are `render_date(frmt, (read_local + timedelta(seconds=s)).date(),
   add_days=DAY, add_months=MON, add_years=YEAR)` for `s` in `(-slack_s, 0, +slack_s)`: around midnight
   either date passes.
3. `got` not among them: `Difference(f"item {n} (CTDAT)", "TXT", <the rendering for read_local>, got)`.

`read_local` is the local time of the read by the time server's clock:
`to_local(readback.read_at + reading.offset_s)` (§9.2). The pipeline calls it after `compare` at every
read-back of a Send (§10.8, §10.10). OPEN: month arithmetic at month ends (§3.2).

### 8.3 fingerprint(design)

The poller compares the running message with what was last sent by fingerprint, plus name and slot
(architecture decision 22). The fingerprint leaves out everything that changes by itself:

```python
items = []
for it in design.items:
    relative = it.value("XRID", "0") not in ("", "0") or it.value("YRID", "0") not in ("", "0")
    items.append([it.tag, [[k, v] for k, v in it.pairs
                           if k != "BOUND"
                           and not (it.tag == "CTDAT" and k == "TXT")
                           and not (relative and k == "POS")]])
obj = {"v": 1, "name": design.name, "empty": design.empty, "width": design.width,
       "height": design.height, "items": items,
       "settings": sorted([k, v] for k, v in design.settings.items())}
return hashlib.sha256(json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
```

Left out: the slot, `read_at`, `tn`, `xid`, the picture and `black_pixels` (the date is in them), every
BOUND, every CTDAT's TXT, and the POS of relative items (the printer works it out from the item before,
whose width can change with the date). Everything else counts, repeats and order included. Identical
replies give identical fingerprints whatever their line endings (protocol.md §12.4).

### 8.4 origin_from_design(design)

The `TXT` of the first CTEXT item whose `TXT` starts with `ORIGIN_PREFIX`, else `""`. No other text may
contain origin words (§2.3), so this finds the origin item in any read-back (architecture decision 23).
For a pre-printed film it returns `""`, and the origin line shown comes from the expected state.

### 8.5 cleared(design)

`design.empty or (not design.items and design.name == "")`: the slot holds nothing that can print. Used
after the end-of-test empty messages (§15.5).

## 9. Time and the printer clock

### 9.1 sntp_query(host, *, port=123, timeout_s=2.0)

One SNTP request over UDP (RFC 4330), standard library only:

1. `socket.getaddrinfo(host, port, type=socket.SOCK_DGRAM)`, first result. Name resolution isn't bound
   by `timeout_s`; that's one reason the time is asked on worker threads only. A failure raises
   `TimeServerError(Msg(N_("{server} didn't answer the time request."), {"server": host}))`.
2. The request is 48 bytes: byte 0 = `0x23` (leap 0, version 4, mode 3, client); bytes 40 to 47, the
   transmit timestamp, hold `t1 = time.time()` in NTP form (seconds since 1900 = Unix + 
   `NTP_EPOCH_OFFSET`, big-endian 32 bits, then a 32-bit fraction); everything else 0.
3. Send it and wait for a reply from that address until `timeout_s` has passed in all (datagrams from
   other addresses are ignored). `t4 = time.time()` on receipt. A timeout raises the "didn't answer"
   error.
4. The reply must be at least 48 bytes; mode (`b[0] & 7`) 4; version (`(b[0] >> 3) & 7`) 3 or 4; leap
   indicator (`b[0] >> 6`) not 3 (unsynchronised); stratum `b[1]` 1 to 15; originate timestamp (bytes 24
   to 31) equal to the bytes we sent at 40 to 47; transmit timestamp (40 to 47) not zero. Otherwise
   `TimeServerError(Msg(N_("{server} sent a time answer that doesn't make sense."), ...))`.
5. `t2` (bytes 32 to 39) and `t3` (40 to 47) to Unix time; `offset = ((t2 - t1) + (t3 - t4)) / 2`;
   return `t4 + offset`, the server's UTC at the moment of receipt.

It uses `time.time()` directly (it measures this PC's real clock against the server); `TimeSource`
relates the answer to its injected clock.

### 9.2 TimeSource

1. `parse_host(h)`: when `h` has exactly one `:` and the part after it is digits, `(before, int(after))`;
   otherwise `(h, SNTP_PORT)`. So `hx_fake.py` can point the app at a fake time server with
   `127.0.0.1:<port>` (protocol.md §16.3).
2. `set_hosts(hosts)` keeps the non-empty ones, in order. Every user of a `TimeSource` calls
   `time.set_hosts([company.time_server, company.time_server_fallback])` from its current `HxData`
   before asking (the Send pipeline at the start of every run, the poller each cycle, `PrinterActions`
   per call).
3. `now(fresh=False)`: if `fresh`, or there is no cached offset, or the cache is at least `cache_s` old
   (by `clock.monotonic()`), ask each host in order with `query(host, port=port,
   timeout_s=self.timeout_s)`. The first good answer `utc` gives `offset_s = utc - clock.time()`, cached
   with the host and the monotonic time. If every host fails (or there are none), the cache is cleared
   and the reading is the PC's: `source="pc"`, `server=""`, `offset_s=0.0`.
4. The reading: `utc = clock.time() + offset_s`, `local = to_local(utc, offset_fn=self.offset_fn)`,
   `source="server"`, `server=<host as configured>`, `offset_s`.
5. `now_from(reading)`: the same `source`, `server` and `offset_s`, with `utc = clock.time() + offset_s`
   and its `local`. No request. A Send asks the server once and uses `now_from` for every machine's
   clock set and date check, so each TD carries the time of its own moment.
6. Thread-safe: one lock around the cache. Queries run outside it.
7. `now()` never raises. The caller warns, and marks results amber, when `source == "pc"`.

### 9.3 Local time and daylight saving

1. `local_utc_offset(utc)` is `float(time.localtime(utc).tm_gmtoff)`: this PC's own time-zone rules
   (Eastern with daylight saving at the plant). Python 3.12 provides `tm_gmtoff` on Windows too.
2. `to_local(utc, *, offset_fn)` is `datetime(1970, 1, 1) + timedelta(seconds=utc + offset_fn(utc))`,
   naive. Tests pass an `offset_fn` instead of changing the PC's time zone.
3. `DstWatcher(clock=, offset_fn=)` records `offset_s = offset_fn(clock.time())` and `previous_s = None`.
   Each `changed()` computes the offset now; when it differs from `offset_s`, it sets `previous_s =
   offset_s`, `offset_s = <new>` and returns True; otherwise False. So it is True exactly once per
   change, on the first call after it. A process started after a change never reports it; for that case
   `recent_dst_change` (item 5) exists.
4. With an offset function that returns −14,400 (EDT) before 1 Nov 2026 06:00 UTC and −18,000 (EST)
   after, a watcher called every 5 s returns True once, on the first call at or after 06:00:00 UTC, with
   `previous_s == -14400.0` and `offset_s == -18000.0`.
5. `recent_dst_change(utc, *, within_s=86_400.0, offset_fn=local_utc_offset) -> tuple[float, float] |
   None` returns `(offset_fn(utc - within_s), offset_fn(utc))` when they differ, else None. It lets a
   poller that started after a change (every PC restarted overnight) still make the one automatic clock
   set: states.md may do it once per printer when a change happened within the last day and the
   printer's clock is off from the time server by the change, give or take `clock_warn_s` (§9.6 item 8).

### 9.4 clock_command(local): the TD command and the midnight rollover

The TD command has no seconds field (manual §4.2.3), so the time is rounded to the nearest minute:

1. `t = local + timedelta(seconds=30)`, then `t = t.replace(second=0, microsecond=0)`. At most 30 s off
   (OPEN: whether the printer zeroes its seconds on TD; at most 60 s off if it doesn't).
2. `t.year` outside 2000 to 2050 raises `ValueError`.
3. `Command.block("TD", [("DAY", t.day), ("MNTH", t.month), ("YEAR", t.year), ("HOUR", t.hour),
   ("MIN", t.minute), ("DTRH", ROLLOVER[0]), ("DTRM", ROLLOVER[1])])`.

Every TD Stripes sends carries the full date, the time and the date rollover, so the date always changes
at midnight (plan). Examples:

| `local` | XML |
|---|---|
| 29 Oct 2026 08:44:31 | `<TD><DAY>29</DAY><MNTH>10</MNTH><YEAR>2026</YEAR><HOUR>8</HOUR><MIN>45</MIN><DTRH>0</DTRH><DTRM>0</DTRM></TD>` |
| 29 Oct 2026 08:44:29 | the same with `<MIN>44</MIN>` |
| 31 Dec 2026 23:59:45 | `<TD><DAY>1</DAY><MNTH>1</MNTH><YEAR>2027</YEAR><HOUR>0</HOUR><MIN>0</MIN><DTRH>0</DTRH><DTRM>0</DTRM></TD>` |

OPEN: the manual gives HOUR and DTRH as 1 to 24 and DTRM as 1 to 59, while its own MIN is 0 to 59 and a
24-hour clock needs 0. The safe default built is HOUR 0 to 23 and the rollover (0, 0), which protocol.md
allows and the fake accepts. If the test unit refuses HOUR 0 or DTRM 0 (answer 3), every TD in the
00:00 to 00:59 hour (or every TD) fails red at the clock step, visibly, and only `ROLLOVER` and the hour
mapping in `clock_command` change. If it accepts them but treats them differently, the clock read-back
(§9.5) and the date check (§8.2) catch it.

### 9.5 The clock set

The same procedure serves a Send's clock step, Set clocks and (implemented by states.md's poller, which
can't import `hx_send`) the daylight-saving clock set. `reading` is a `TimeReading` from
`TimeSource.now(fresh=True)`.

1. Set clocks and the daylight-saving set only: read DATE and TIME first (`client.read`) and keep them
   as "before" for the log. A Send skips this (the poll status already shows the clock offset).
2. `cmd = clock_command(time.now_from(reading).local)`; `lock.check()` when a lock is held;
   `client.command(cmd)`. Any error is the step's failure; the TD is never repeated.
3. `d = client.read("DATE")`, `t = client.read("TIME")`, `printer = hx_protocol.clock_from(d, t)`,
   `expected = time.now_from(reading).local`.
4. `abs((printer - expected).total_seconds()) <= CLOCK_SET_TOLERANCE_S`: done. Otherwise read DATE and
   TIME once more (a pair read across midnight can be a day out) and check again. Still off: raise
   `CheckFailed(Msg(N_("{machine}'s clock reads {printer_time} after it was set to {expected}."),
   {...}), differences=[Difference("clock", "DATE TIME", expected.strftime("%Y-%m-%d %H:%M"),
   printer.strftime("%Y-%m-%d %H:%M:%S"))], step=STEP_SET_CLOCK)`.
5. The step's record, kept for the log: `{"sent": <the TD's rounded date and time as "%Y-%m-%d %H:%M">,
   "before": <"%Y-%m-%d %H:%M:%S"> or None, "after": printer.strftime("%Y-%m-%d %H:%M:%S"),
   "difference_s": round(<printer minus expected, seconds>, 1), "source": reading.source, "server":
   reading.server, "offset_s": round(reading.offset_s, 3)}`.

### 9.6 The daylight-saving clock set (the poller's one write)

Just after each daylight-saving change, the PC polling each printer re-sets its clock, logged as
automatic (plan). This is the only write Stripes makes that no person started. states.md owns the
poller and the event-log entry; these are the rules it must follow:

1. The poller's coordinator calls `dst.changed()` once per heartbeat (every `heartbeat_s`, so within 5 s
   of the change). On True it marks every printer this PC is polling at that moment as due, with the
   change `(previous_s, offset_s)`.
2. The printer's worker handles the due mark at its next cycle, after the usual lock read before the
   request. While a lock is held (on the share or in `local_locks`), the mark waits; the first cycle
   without a lock does it. A worker that stops polling the printer drops the mark.
3. It takes no lock: it is already the only PC talking to that printer (architecture §7.3 rule 6).
4. `reading = time.now(fresh=True)`. With no answer from the time server it uses the PC's time (the PC
   follows the domain's time) and logs `source "pc"`.
5. The clock set of §9.5, steps 1 to 4. One TD, never repeated, even when it fails.
6. One hx-events entry (type and writer owned by states.md), carrying at least `printer_id`,
   `machine_id`, `previous_offset_s`, `offset_s`, the §9.5 record, `ok` and `msg`.
7. The poll status records `daily["dst_clock_set"] = {"offset_s": offset_s, "at": <time>}` so a poller
   that takes the printer over later doesn't do it again for the same change.
8. Optional catch-up (states.md decides): a poller that starts polling a printer, finds no
   `daily["dst_clock_set"]` for the current offset, gets a `recent_dst_change(...)`, and reads a printer
   clock offset within `clock_warn_s` of `new − old` (for example +3,600 s after the autumn change), does
   items 4 to 7 once. Still one write per change and printer.

## 10. The Send pipeline

### 10.1 Overview

`SendPipeline.run(plan, on_event, *, cancel=None) -> SendResult` runs on the UI's `hx-send-<send_id>`
thread. That thread does the whole-Send work (check, locks, countdown, time, finishing); each machine
runs its own steps on `hx-send-<send_id>-<machine_id>`. Every machine of every chosen line runs at the
same time, with one request at a time to each printer (the gate and the lock). `on_event` is called from
all these threads; the UI passes `bus.post`.

| Time (default timings) | Whole Send | Each machine |
|---|---|---|
| 0 s | check the choice (§10.2), take the locks (§10.3), start the countdown, ask the time server | wait for the poller to step aside (§10.4) |
| 5 s | countdown over: "go", Cancel removed | first request: check the machine (§10.5) |
| about 8 s | | set the clock, build in the idle slot, check it (§10.6 to §10.8) |
| about 11 s | | record the switch, switch, printing on (§10.9) |
| about 18 s | | confirmed twice (§10.10); expected state written, lock released |
| after the last machine | line statuses, the send log, `SendFinished` (§10.11) | |

A machine spends about 13 s at its printer with the real printer's 0.8 s web replies: its four Loads take
about 9 s. The plan's "roughly 5 to 10 seconds" assumed faster web reads; the steps don't change.

The step constants are architecture's `STEPS`. Each machine stops at the first step that fails and does
nothing else; the other machines carry on (§12).

### 10.2 Step check_choice (whole Send)

1. `ok, msg = data.can_send()` (the settings not loaded yet, the 24-hour rule, a newer-format config):
   not ok raises `PlanError(msg, problems=[msg])`. Then `identity.is_sender(data.current.company)` false
   raises `NotAllowed` ("Only Stripes MapleJet Senders can send."). Nothing is posted.
2. Post `SendEvent(send_id, "", STEP_CHECK_CHOICE, "running")`.
3. `data.current` is read once here; the whole run uses that one `HxData`.
4. `fresh = merge_plan(plan.request, data.current, identity)`; its `PlanError` is raised as it is.
5. The plan the sender looked at must still be what would be sent: the same machines in the same
   order, and for each machine the same `content_code`, name and `expected_settings` from
   `build_message(..., 9)`, and the same `origin_line`. Otherwise raise `PlanError` "The templates
   changed since you checked them. Look at the Send window again."
6. `time.set_hosts([company.time_server, company.time_server_fallback])`. Post `ok`. The run carries on
   with `fresh`.

A `PlanError` or `NotAllowed` is the only exception `run` raises; nothing has been written anywhere.
The UI shows the problems and keeps the Send window open.

### 10.3 Step lock and the countdown (whole Send)

1. Post `SendEvent(send_id, "", STEP_LOCK, "running", at=clock.time())` and note `start =
   clock.monotonic()`. The Send window counts `timings.countdown_s` down from that event's `at`.
2. For each line in plan order, for each of its machines in order: `lock = store.lock(printer.id,
   purpose="send", run_id=send_id, line_id=line.id, machine_id=machine.id)` and `state =
   lock.acquire()`:

   | Outcome | What happens |
   |---|---|
   | `"held"` | `lock.start_keeping()` |
   | `"local_only"` (the share didn't answer) | the machine carries on with the note `no_lock`; a `warning` event with `NOTE_TEXTS["no_lock"]` |
   | `LockHeld(info)` | the whole line isn't sent: release the locks already taken for it; every machine of the line is red at `STEP_LOCK` with the holder's message (below), a `failed` event each |
   | any other `ShareError` (such as `ShareDenied`) | the same, with the error's own message |

   The holder's message depends on `info.purpose`: `send` gives "{line} is being sent by {user}."
   (the plan's "Line 2 is being sent by jsmith"); `test` and `calibration` give "{machine} is running a
   test print by {user}."; `stop`, `resume` and `clock` give "{machine} is being changed by {user}.";
   anything else (a damaged lock) uses `LockHeld`'s own message. `{user}` is `info.user` and
   `{machine}` the machine whose printer is locked.
3. For each machine still going: `store.read_expected(machine_id)`. `FormatTooNew`: that machine is red
   at `STEP_LOCK` with its message ("Update Stripes: ...") and its lock is released. `ShareUnavailable`:
   the note `no_lock`. Otherwise the result is kept as the machine's state before the Send.
4. Start the machine threads. Each begins with step wait_poller (§10.4), which makes no printer request.
5. On this thread, `reading = time.now(fresh=True)` (at most 2 s per host, inside the countdown).
6. Wait until `timings.countdown_s` has passed since `start`, in slices of at most 0.25 s with
   `clock.sleep(slice, cancel)`.
7. **Cancelled** (the token fired at any point up to here): stop the machine threads (they have sent
   nothing), join them, release every lock, and make every machine not already red
   `MachineResult(colour=RED, ok=False,
   step=STEP_LOCK, msg="Cancelled. Nothing was sent.", notes=["cancelled"])`, with a `skipped` event
   each. No expected state, line status or log is written. Post `SendFinished` and return. `is_cancelled`
   tells the UI to close quietly instead of opening the red window.
8. **Go.** If `reading.source == "pc"`, every machine gets the note `pc_time` and a `warning` event
   `SendEvent("", STEP_SET_CLOCK, "warning", NOTE_TEXTS["pc_time"])` is posted. If
   `plan.from_local_copy`, every machine gets the note `local_copy`. Post `SendEvent("", STEP_LOCK,
   "ok")`: the window shows "Hang on, we're working on it" and removes Cancel. From here `cancel` is
   ignored (architecture §7.5).
9. Join every machine thread, then finish (§10.11).

### 10.4 Step wait_poller (each machine)

Before its first request to a printer, the sender waits until that printer's poller reports "paused
for lock", or its counter goes stale (plan; storage.md §11.6). The countdown usually covers it.

1. Post `running`. A `local_only` lock: no handshake is possible; post `skipped` and go to step 4.
2. `deadline = clock.monotonic() + 2 * timings.stale_s`. Loop, sleeping `timings.lock_refresh_s / 20`
   between calls (and leaving at once when the Send is cancelled):

   | `store.check_poller_paused(machine_id, lock.token)` | Next |
   |---|---|
   | `"paused"` | ready |
   | `"stale"` or `"missing"` | call it again at once: `"stale"`, `"missing"` or `"paused"` again means ready, `"polling"` means keep waiting |
   | `"polling"` | keep waiting |
   | raises `ShareUnavailable` | the note `no_lock`; ready (the share is down, as in §7.3 rule 7) |

   Past the deadline: red at `STEP_WAIT_POLLER`, "Waited {seconds} s for the PC polling {machine} to
   step aside. Try again." (`seconds` = `round(2 * stale_s)`).
3. Post `ok`.
4. Wait for "go" (§10.3 step 8). Then, unless the lock is `local_only` or the share failed in step 2,
   call `check_poller_paused` once more; `"polling"` sends the machine back to the step 2 loop, with the
   same deadline. Only then does the first request go out.

A machine that has never been polled has no `poll.json`; "missing" needs `stale_s` without a break, so
its first Send waits up to 15 s longer. That is the safe side of the not-found cache (storage.md §11.6).

### 10.5 Step check_machine

Before every request of every step, `lock.check()`. `LockLost` makes the machine red at the current step
with its message; nothing more is sent to that printer and its expected state isn't written (the lock
is someone else's now).

1. Post `running`. `client = clients.client(printer)`: `AuthFailed` is a failure.
2. `client.check_uid()`: `WrongPrinter` is a failure (the message names both UIDs).
3. Encoder machines whose record has `encoder_wheel_mm` and `encoder_ppr`: `wheel = client.read("ENWHD")`,
   `ppr = client.read("ENPPR")`. `wheel != round(encoder_wheel_mm)` or `ppr != encoder_ppr` fails:
   "{machine}'s encoder settings changed: wheel {wheel} mm, {ppr} pulses per turn. Every print would
   move. Ask an administrator."
4. `fonts = client.fonts()`. Any error, or `fonts != company.standard_fonts` (a list shorter, longer or
   different in any name or position), fails: "{machine}'s printer has fonts that differ from Stripes'
   standard list." The same font number means different fonts on different printers, so a different
   list never passes.
5. `ink_name = client.read("INKT")`. `None` fails: "{machine} reports no ink." No `Ink` in
   `company.inks` with exactly that `name` fails: "{machine} reports ink "{ink}", which isn't on Stripes'
   ink list." Then the colour, then the cartridge, each failing with the note `ink_mismatch`:
   - `ink.colour != film.ink_colour`: "{machine} is set for black ink; this film needs white." or
     "{machine} is set for white ink; this film needs black." (plan wording; the machine is skipped and
     flagged).
   - `ink.cartridge != film.cartridge`: "{machine} is set for paper cartridges; this film needs plastic."
     or the other way round.

   OPEN: whether the ink name follows a cartridge swap or is only the cartridge setting (plan, risks).
6. `printing = client.read("PRIN") == 1`.
7. `current = client.load_slot(0)`: the message printing now, read from the printer itself; its `slot`
   is the printing slot. `slot = idle_slot(printing, current.slot)`. The machine's previous message is
   `previous = RunningMessage(slot=current.slot, name=current.name, fingerprint=fingerprint(current),
   origin_line=origin_from_design(current) or <the expected state's origin_line when it names the same
   fingerprint>, read_at=current.read_at, preview_sha256=sha256(current.preview_bmp).hexdigest())` and
   `previous_preview = current.preview_bmp`.
8. `counts_before = client.read(company.bag_counter)` unless it is `"none"`. A failure here leaves it
   None and doesn't stop the machine: the count is only for the log.
9. Post `ok` with `design=current`.

If the step fails before item 7, `previous` and `previous_preview` come from the poll status instead
(`store.read_poll(machine_id).running` and `store.read_preview(machine_id)`, best effort, errors
ignored), so the red window can still show what the machine was last seen printing.

### 10.6 Step set_clock

The clock set of §9.5 (without the "before" read), with the run's `reading`. The date, the time and the
midnight rollover are set at every Send (plan). Post `ok`, or `warning` with `NOTE_TEXTS["pc_time"]`
when `reading.source == "pc"` (the result is amber).

### 10.7 Step build

1. Post `running`. `built = build_message(machine_plan, slot)`.
2. For each command of `built.commands` in order (CRTMSG, each item, CPPARM, SAVEMSG slot):
   `lock.check()`, `client.command(cmd)`. `CommandRefused`, `NoReply` or `BadReply` fails the machine.
   A failure here is before the switch: the machine is still printing its previous message, because
   only the idle slot was written.
3. Post `ok`.

`slot` is never the slot that was printing at step 7 of §10.5, so SAVEMSG never goes to the printing
slot (the fake records any such write as a violation).

### 10.8 Step verify_idle (read-back 1)

1. Post `running`. `d = client.load_slot(slot)`; `d` is appended to `readbacks`.
2. `diffs = compare(built, d) + date_differences(built, d, to_local(d.read_at + reading.offset_s))`.
3. Any difference raises `CheckFailed(msg, differences=diffs, step=STEP_VERIFY_IDLE)` with "The date
   {machine} would print differs from the date Stripes expects." when every difference is a date's TXT,
   and otherwise "The message read back from {machine} differs from what was sent ({n} differences)."
   The machine stops here, red, still printing its previous message.
4. Post `ok` with `design=d`.

### 10.9 Step switch

1. Post `running`.
2. Record "switch pending" in the machine's expected state (§14, row "Switch pending"): the expected
   message name and content code, the slot, the origin line and what was printing before. Only with a
   `held` lock; a share failure here adds the note `no_lock` and the Send carries on (the plan's
   sending without the share).
3. Record "switch attempted" in the send log: `data.append_send_log(<send_switch entry>, §17.3)`. It
   queues on this PC when the share is down, so it fails only if this PC can't save it either; that
   fails the machine before the switch.
4. From here the machine is "after the switch": `after_switch = True` in every result and failure.
5. `client.command(Command.set("PRINTMSG", slot))`, then `client.command(Command.set("PRINT", 1))`.
   PRINT 1 is sent whether printing was on or off.
6. Post `ok`.

### 10.10 Steps confirm_1 and confirm_2 (read-backs 2 and 3)

For each of the two:

1. Post `running`. `d = client.load_slot(0)`, each reply matched to its own request by TN and XID
   (protocol.md §10.6); `d` is appended to `readbacks`.
2. `d.slot != slot` fails with "{machine} is printing slot {got}, not slot {slot}." Otherwise `compare`
   and `date_differences` as in §10.8, failing the same way.
3. `client.read("PRIN") != 1` fails with "{machine} didn't turn printing on." So "printing on" must read
   1 twice in all.
4. Post `ok` with `design=d`.

Between the two: `clock.sleep(timings.confirm_gap_s)`. After confirm_2: `counts_after =
client.read(company.bag_counter)` as in §10.5 step 8. The machine has passed: write its expected state
(§14, row "Live, not yet confirmed", with the fingerprint of the second read-back), release its lock,
and post `SendEvent(send_id, machine_id, STEP_OPERATOR, "running")`: waiting for the sender. Stripes
keeps the second picture for the sender.

### 10.11 Finishing (whole Send)

After every machine thread has ended:

1. Line statuses (§14): one `LineStatus` per line of the plan, except a line whose machines all failed
   at `STEP_LOCK` (nothing happened at its printers). Written with `store.write_line_status`; a share
   error is printed to stderr and ignored, because line statuses are informational.
2. The send log: `log_name, log_queued = data.append_send_log(<send entry>, §17.2)`.
3. Post `SendFinished(result)` and return the `SendResult`.

Every machine's lock is released by its own thread, after its last expected-state write, whatever
happened. An unexpected exception in a machine thread is caught at the top of the thread, printed with
its traceback, logged to hx-events (`internal_error`, §17.6) and turns that machine red at its current
step with "Stripes had an internal error at this step: {error}" (`error` = the exception's class name,
never its text, which could hold bag text).

### 10.12 Results

| `MachineResult` field | Value |
|---|---|
| `colour` | `AMBER` when the machine passed confirm_2 (live and checked, not yet confirmed by the sender), else `RED` |
| `ok` | True when it passed confirm_2 |
| `step` | the step it failed at, or `STEP_CONFIRM_2` |
| `msg` | the failure's `Msg`; None when ok |
| `after_switch` | PRINTMSG was sent (even without a reply) |
| `slot`, `message_name`, `content_code` | the idle slot and the built message; `None`/`""` when not reached |
| `fingerprint` | of the confirm_2 read-back; `""` otherwise |
| `origin_line` | the plan's (bag text) |
| `readbacks` | idle slot, running 1, running 2, as far as reached (a failed read-back is included) |
| `previous`, `previous_preview` | §10.5 step 7 |
| `counts_before`, `counts_after` | §10.5 step 8, §10.10 |
| `notes` | in this order as they apply: `pc_time`, `no_lock`, `local_copy`, `ink_mismatch`, `film_stop`, `cancelled` |

`SendResult`: `send_id`; `sender = identity.user`; `computer = identity.computer`; `started_at` and
`finished_at` by the time server's clock (`clock.time() + reading.offset_s`; plain `clock.time()`
before the reading exists); `machines` in plan order; `time_source = reading.source`;
`from_local_copy` and `data_as_of` from the plan; `log_name` and `log_queued` from step 2 of §10.11.

Colours the operator sees (plan): **green** once the sender confirms "Everything is right" and no amber
note applies (§11); **amber** while live but not confirmed, or confirmed with an amber note
(`pc_time`, `no_lock`, `local_copy`); **red** for not sent, failed or rejected.

### 10.13 How each failure is handled

This fills in architecture §8's table. "Written" is the machine's expected state (§14), only while the
lock is `held`.

| Step | Failure | Result | Written |
|---|---|---|---|
| before anything | not a sender | `NotAllowed` raised | nothing |
| check_choice | `can_send` false, `PlanError`, templates changed | `PlanError` raised | nothing |
| lock | `LockHeld`, another `ShareError` | the line's machines red, not sent | nothing |
| lock | `FormatTooNew` on the expected state | that machine red ("Update Stripes") | nothing |
| lock, wait_poller | `ShareUnavailable` | carries on with the note `no_lock` (amber) | nothing it can't write |
| wait_poller | the poller didn't step aside in time | red | "Failed before the switch" |
| check_machine | `AuthFailed`, `WrongPrinter`, `NoReply`, `PrinterBusy`, `BadReply`, `WebReadFailed`, fonts, ink, encoder | red; still printing its previous message | "Failed before the switch" |
| set_clock | `NoReply`, `CommandRefused`, `BadReply`, `CheckFailed` | red; still printing its previous message | "Failed before the switch" |
| build | `NoReply`, `CommandRefused`, `BadReply` | red; still printing its previous message | "Failed before the switch" |
| verify_idle | `CheckFailed`, `WebReadFailed`, `NoReply` | red; still printing its previous message | "Failed before the switch" |
| switch, before PRINTMSG | the switch entry can't be saved even on this PC | red; still printing its previous message | "Failed before the switch" |
| switch, confirm_1, confirm_2 | anything | red, `after_switch`: "This machine may be printing the new message, the old one, or nothing. Check the bags now." | "Failed after the switch" |
| any | `LockLost` | red | nothing (the lock isn't ours) |
| any | an unexpected exception | red, "internal error" | as for the step |

No step retries a write. The only automatic repeat is the read retry inside `PrinterClient` (a read
that gets busy or no reply is asked once more within a second, protocol.md §8). Stripes never switches
back, turns printing off or tries again on its own: the connection can't be trusted at that point.

### 10.14 Requests per machine

With a three-item print text: check_machine 8 (HXUI, the font file, INKT, PRIN, a Load and its repeat,
the counter, plus the web login the first time), 2 more on encoder machines (ENWHD, ENPPR); set_clock 3
(TD, DATE, TIME); build 6 (CRTMSG, 3 items, CPPARM, SAVEMSG); verify_idle 2; switch 2 (PRINTMSG, PRINT),
plus a warm-up HXUI when the share writes before it took 3 s or more; confirm 7 (two Loads with their
repeats, PRIN twice, the counter). About 30 requests, as the plan says. Every one is sent through the
printer's gate with the lock held, so the printer never sees two at once.

## 11. The sender's confirmation

### 11.1 Who and when

1. After the Send, the result window shows each live machine's fresh picture (its confirm_2 read-back)
   with its origin in large type. Only the person who sent it confirms, pressing "Everything is right"
   or "Something's wrong" (plan). Training covers waiting for the first bags.
2. If they close the window, they reopen it from their monitor card: `MachineView.confirm_pending_by`
   and `confirm_send_id` (states.md) name the sender and the Send; the card offers it only when
   `same_user(confirm_pending_by, identity.user)`. `ResultWindow.from_log` calls
   `data.find_send(send_id)`, `result_from_log(entry)` and `pipeline.pending_confirmation(send_id)`.
3. Until they confirm, the line shows amber "Send not confirmed" (states.md, from the expected state).

### 11.2 confirm(send_id, machine_ids, everything_right)

Blocking; worker thread.

1. `entry = data.find_send(send_id)`. None raises `ShareError(Msg(N_("Stripes can't find this Send yet.
   Try again in a minute.")))`.
2. `not same_user(identity.user, entry["sender"])` raises `NotAllowed(Msg(N_("Only {sender} can confirm
   this Send.")), {"sender": entry["sender"]})`.
3. Only machines of the entry that went live (`ok`) count; other ids are ignored.
4. Log first, so the answer is never lost: `data.append_send_log(<send_confirm entry>, §17.4)`, its own
   add-only file (queued on this PC if the share is down).
5. For each machine, one at a time: `with store.lock(printer_id, purpose="send", run_id=send_id,
   line_id=..., machine_id=...)` (no poller handshake: no printer request is made), read the expected
   state, and only if `send_id` still matches, `mode == "running"`, it isn't confirmed or rejected and
   has no failure, write the row "Confirmed" or "Rejected" of §14. Otherwise leave it: a later Send,
   Stop or test has replaced it (architecture decision 24). `LockHeld` (someone is working on that
   printer now) or a `local_only` lock or a share error: the machine isn't updated.
6. For each line whose `LineStatus.send_id == send_id`: set the machines' colours, and `confirmed =
   True`, `confirmed_by = identity.user` when the answer was "Everything is right" and every machine of
   that line in the Send that went live is now confirmed.
7. If any machine wasn't updated because of step 5's lock or share cases, raise
   `ShareError(Msg(N_("Your answer was logged, but Stripes couldn't update the monitor for
   {machines}.")), {"machines": <names joined with ", ">})` after doing everything else. The UI offers
   to try again; a second confirm writes a second log entry, which is harmless.

"Something's wrong" continues with the Stop question (§13.1).

### 11.3 pending_confirmation(send_id)

Blocking. `data.find_send(send_id)` (None gives `[]`), then for each machine of the entry that went live:
`store.read_expected(machine_id)`; the machine is pending when `send_id` matches, `mode == "running"`,
it isn't confirmed or rejected and has no failure. Share errors are raised.

## 12. When a step fails

### 12.1 Partial failure across a line

That machine stops at the step that failed. The other machines carry on, and those that pass go live,
so a line can end up printing two origins until it is fixed (plan). The sender and the line's notify
user are told: the red window here, alerts with sound from states.md (from the failure in the expected
state). The floor board lists each machine's origin when they differ (states.md, ui.md).

### 12.2 The red window

The full-screen red window (ui.md) opens when a Send ends with any machine red, except a cancelled
Send. Its content comes from `failure_rows(result, plan)`: one row per `MachineResult` that isn't ok and
isn't cancelled, in result order:

| `FailureRow` field | From |
|---|---|
| `machine_id`, `machine_name`, `line_name` | the plan's records |
| `step`, `step_text` | `r.step`, `STEP_TEXTS[r.step]` |
| `msg` | `r.msg` |
| `after_switch`, `after_switch_text` | `r.after_switch`; when true, "This machine may be printing the new message, the old one, or nothing. Check the bags now." |
| `previous_name`, `previous_origin`, `previous_preview` | what it was last confirmed printing: `r.previous.name`, `.origin_line` and `r.previous_preview`, or `""`/`b""` |
| `differences` | when the failed step was a read-back: `compare(build_message(<its machine plan>, r.slot), r.readbacks[-1])` |

The window shows each row's picture with its origin in large type, and waits for Try again or Leave
it. It does nothing on its own.

### 12.3 Try again

Try again is a fresh Send of the failed machines from step 2, never a resume halfway (plan):
`new_plan = plan.restricted([row.machine_id for row in rows])`, then `SendPipeline.run(new_plan, ...)`
on a new `hx-send-<id>` thread, with its own countdown, locks and checks, and a new `send_id`. Its step
1 is the same quick check as any Send's (§10.2): it touches no printer, and it stops the retry if the
templates changed meanwhile. A machine that failed after the switch is rebuilt in whichever slot is idle
now, read from the printer again.

### 12.4 leave_it(send_id, machine_ids)

Leave it keeps the line red and is logged (plan). Blocking.

1. `identity.is_sender(company)` false: `NotAllowed`.
2. `token = secrets.token_hex(8)`.
3. `data.append_send_log(<send_leave entry>, §17.5)`.
4. For each machine, under the printer lock as in §11.2 step 5: when `send_id` still matches, write the
   row "Leave it" of §14 (who, when, the new `reread_token`). The result stays red and the failure stays.
5. Share or lock problems: the §11.2 step 7 `ShareError`.

A new `reread_token` asks the assigned poller to load the running message straight away (states.md), so
the red card shows what's really printing.

### 12.5 A PC that crashes mid-Send

Its locks go stale after 60 s and the poller checks what's running (states.md). What the pipeline
leaves for it: the expected state's "switch pending" row (written before PRINTMSG, with the new and the
previous message names and the previous fingerprint), and the `send_switch` log entry. Before that row
is written, only the idle slot and the clock have changed, and the printer is still printing its
previous message.

## 13. Something's wrong, Stop, Resume and Set clocks

`PrinterActions` runs on the UI's `hx-action-<action_id>` thread, one machine per thread
`hx-action-<action_id>-<machine_id>`, all at the same time. Every method needs
`identity.is_sender(company)` and otherwise raises `NotAllowed(Msg(N_("Only Stripes MapleJet Senders
can stop, resume or set clocks.")))`. `action_id = new_id("ac")`. Each machine takes its printer's lock
(purpose `stop`, `resume` or `clock`, `run_id = action_id`), handles `LockHeld` with the holder's message
of §10.3, waits for the poller as in §10.4 (without a countdown: "go" is immediate), and calls
`lock.check()` before every request. An `ActionEvent` is posted when each machine starts (`running`)
and ends (`ok`, `failed` or `warning`), then `ActionFinished`.

`MachineResult` in an `ActionResult`: `colour` GREEN when done, AMBER when done with an amber note
(`no_lock`, `pc_time`), RED when not done; `step` is `STEP_LOCK`, `STEP_WAIT_POLLER`,
`STEP_CHECK_MACHINE`, `ACTION_STOP`, `ACTION_RESUME` or `STEP_SET_CLOCK`.

### 13.1 When the sender says something's wrong

1. "Something's wrong" calls `confirm(send_id, machine_ids, everything_right=False)` (§11.2): the
   machines become red and rejected, and Resume is blocked for them.
2. The UI asks "Stop printing on Bagger 3 and 4?" with Stop printing and Go back (ui.md's texts).
3. Stop printing: `actions.stop(machine_ids, on_event, reason="rejected", send_id=send_id)`.
4. When every machine stopped, the window moves on: "Printing stopped on Bagger 3 and 4. Line 2 needs a
   new Send." Its main button, Fix and send again, opens the Send window with the previous choices
   filled in: `nav.open_send(prefill=request)` with the plan's request, or `request_from_log(entry)`
   when the window was reopened from the log. The Send window gives the prefill a new `send_id`.
5. Closing instead leaves the line red as "Stopped, needs a Send", and the line's notify user is
   alerted (states.md, from `stop_reason == "rejected"` and `LineStatus.stopped_needs_send`).
6. A machine that couldn't be stopped is red with "Stripes couldn't stop printing on {machine}. Stop it
   at the printer with its Print On/Off button now.", and the window offers Try again, which calls
   `stop` again for those machines.
7. Go back leaves the machines rejected and red, still printing, with Resume blocked; the notify user is
   alerted (states.md).

### 13.2 stop(machine_ids, on_event, *, reason="user", send_id="")

Per machine:

1. Lock (purpose `stop`); a `local_only` lock carries on with the note `no_lock`. Wait for the poller.
2. `client.check_uid()`.
3. `client.command(Command.set("PRINT", 0))`.
4. `client.read_twice("PRIN")` must be 0 (two readings a second apart that agree, plan: "reads twice to
   confirm it's off").
5. Done: the expected state's row "Stopped" (§14). Any failure in steps 2 to 4: red with the "Stop it at
   the printer" message, and the row "Stop requested at the printer". Both only with a `held` lock.
6. Release the lock.

Then, for `reason == "rejected"`: every line whose `LineStatus.send_id == send_id` gets
`stopped_needs_send = True`. Then the `stop` log entry (§17.6) and `ActionFinished`. Stop works without
the 24-hour rule: it is a safety action, and it needs only the machine and printer records already
loaded.

### 13.3 The Resume rules

"Stop printing and Resume printing, for a line or one machine. Resume first loads the message that's on
the printer and shows its picture and origin to confirm. It refuses if that message differs from the
last confirmed Send, or if the last Send was rejected" (plan). Resume is also blocked after a test print,
and after any failed Send.

`resume_refusal(expected, machine)` (pure; states.md may use it for `MachineView.can_resume`), first
match wins:

| Expected state | Refusal |
|---|---|
| None, or `mode == "none"` | "No Send is recorded for {machine}. Send instead." |
| `mode` is `switch_pending` or `test_running` | "{machine} is in the middle of a Send or a test." |
| `rejected` | "The last Send to {machine} was rejected. Send again instead." |
| `mode == "test_finished"` or `stop_reason == "test"` | "{machine} needs a Send after the test print." |
| `resume_blocked`, or `failure` is set | "{machine} needs a new Send before it can print again." |
| not `confirmed` | "The last Send to {machine} wasn't confirmed. Send again instead." |
| otherwise (`running`, `stopped_by_user`, `stop_requested_at_printer`) | None: allowed |

`resume_mismatch(expected, design, machine)`: None when `design.name == expected.message_name`,
`design.slot == expected.slot` and `fingerprint(design) == expected.fingerprint`; otherwise "The message
on {machine} isn't the last confirmed Send. Send again instead."

### 13.4 check_resume(machine_id)

Blocking; for the Resume dialog.

1. `expected = store.read_expected(machine_id)`. `ShareUnavailable` returns `ResumeCheck(allowed=False,
   design=None, origin_line="", msg="Stripes can't reach the server, so it can't check the last confirmed
   Send.")`: Resume needs the expected state.
2. `resume_refusal(...)` not None: `ResumeCheck(allowed=False, design=None,
   origin_line=expected.origin_line if expected else "", msg=<it>)`. No printer request.
3. Lock (purpose `resume`); a `local_only` lock refuses as in step 1. Wait for the poller,
   `check_uid()`, `design = client.load_slot(0)`, release the lock.
4. `ResumeCheck(design=design, origin_line=origin_from_design(design) or expected.origin_line,
   allowed=(mismatch is None), msg=mismatch)` with `mismatch = resume_mismatch(...)`. A printer error
   gives `allowed=False` with its message and `design=None`.

The dialog shows the picture and the origin in large type and asks the sender to confirm.

### 13.5 resume(machine_ids, on_event)

Per machine, everything again under the lock, because things may have changed since `check_resume`:

1. Lock (purpose `resume`); `local_only` is red with the "can't reach the server" message. Wait for the
   poller.
2. `expected = store.read_expected(machine_id)`; `resume_refusal` refuses: red with it.
3. `check_uid()`, `design = client.load_slot(0)`; `resume_mismatch` refuses: red with it.
4. `client.command(Command.set("PRINT", 1))`; `client.read_twice("PRIN")` must be 1, else red with
   "{machine} didn't turn printing on."
5. The expected state's row "Resumed". Colour `result_colour(expected.confirmed, expected.notes)`.
6. Release the lock. Then the `resume` log entry (§17.6), with each machine's picture.

### 13.6 set_clocks(machine_ids, on_event)

Sets each printer's clock from the time server, without a Send (plan). `Navigator.set_clocks(line_ids)`
passes the active machines of those lines.

1. `time.set_hosts(...)`, then `reading = time.now(fresh=True)`, once for all machines.
   `reading.source == "pc"`: every machine gets the note `pc_time` (amber), and the PC's time is used
   (plan: "If the time server doesn't answer, Stripes warns and uses the PC's time").
2. Per machine: lock (purpose `clock`; `local_only` carries on with `no_lock`), wait for the poller,
   `check_uid()`, then the clock set of §9.5 including the "before" read.
3. Done with `reading.source == "server"` and a `held` lock: the expected state's row "Clock set from
   the server", which clears an earlier `pc_time` note.
4. Release the lock. Then the `clock_set` log entry (§17.6).

## 14. Expected state and line status writes

`store.write_expected` sets `counter`, `written_at`, `written_by` and `computer` itself (storage.md
§5.4). Every write is made while holding that printer's lock in state `held`: with a `local_only` lock,
or when the share fails, nothing is written. Fields not named in a row keep their value.

| Row | When | Fields set |
|---|---|---|
| Failed before the switch | Send: a failure in wait_poller, check_machine, set_clock, build, verify_idle, or in switch before PRINTMSG | `send_id` = this Send; `result` RED; `failure = FailureInfo(step, msg, after_switch=False, at)`; `notes` = this Send's notes |
| Switch pending | Send, §10.9 step 2 | `mode "switch_pending"`; `printing_expected` True; `send_id`; `message_name`, `content_code`, `slot` and `origin_line` of the new message; `fingerprint ""`; `previous_message_name` and `previous_fingerprint` from the slot 0 read of §10.5; `result ""`; `failure` None; `notes`; `confirmed` False, `confirmed_by ""`, `confirmed_at` None; `rejected` False; `resume_blocked` False; `left_by ""`, `left_at` None; `test` None; `stop_reason ""`, `stopped_by ""`, `stopped_at` None |
| Failed after the switch | Send: a failure in switch after PRINTMSG was sent, confirm_1 or confirm_2 | `mode` stays `"switch_pending"`; `result` RED; `failure = FailureInfo(step, msg, after_switch=True, at)` |
| Live, not yet confirmed | Send, §10.10 | `mode "running"`; `fingerprint` of the confirm_2 read-back; `result` AMBER; `failure` None; `previous_message_name ""`, `previous_fingerprint ""` |
| Confirmed | confirm, "Everything is right" | `confirmed` True; `confirmed_by`; `confirmed_at`; `result = result_colour(True, notes)` (GREEN, or AMBER while an amber note applies) |
| Rejected | confirm, "Something's wrong" | `rejected` True; `resume_blocked` True; `result` RED; `failure = FailureInfo(STEP_OPERATOR, "{user} said something is wrong with this Send.", after_switch=True, at)` |
| Leave it | leave_it | `left_by`; `left_at`; `reread_token` (new) |
| Stopped | stop, PRINT 0 confirmed | `mode "stopped_by_user"`; `printing_expected` False; `stop_reason` = the reason; `stopped_by`; `stopped_at`; `resume_blocked = resume_blocked or reason == "rejected"` |
| Stop requested at the printer | stop failed | as Stopped, but `mode "stop_requested_at_printer"` |
| Resumed | resume done | `mode "running"`; `printing_expected` True; `stop_reason ""`, `stopped_by ""`, `stopped_at` None |
| Clock set from the server | set_clocks done with `source "server"` | `notes` without `"pc_time"`; when `mode == "running"` and `result` is GREEN or AMBER, `result = result_colour(confirmed, notes)` |
| Test running | test print: the start, then each step's switch | `mode "test_running"`; `printing_expected` True; `send_id ""`; `message_name`, `content_code`, `slot` of the step; `fingerprint` of its running read-back (`""` at the start); `origin_line ""`; `previous_message_name`, `previous_fingerprint` from the slot 0 read at the start (kept for the whole test); `result ""`; `failure` None; `notes []`; `confirmed` False, `confirmed_by ""`, `confirmed_at` None; `rejected` False; `resume_blocked` True; `left_by ""`, `left_at` None; `test = {"run_id", "kind", "by", "step", "of"}`; `stop_reason ""` |
| Test finished | test print end, printing confirmed off | `mode "test_finished"`; `printing_expected` False; `message_name ""`, `content_code ""`, `fingerprint ""`, `slot` None; `resume_blocked` True; `stop_reason "test"`; `stopped_by` the tester; `stopped_at`; `test` with `"step"` = steps done and `"ended_at"` |
| Test not stopped | test print end, printing not confirmed off | as Test finished, but `mode "stop_requested_at_printer"` |

`at` and the other times written are by the time server's clock when a reading is at hand
(`clock.time() + offset_s`), else `clock.time()`.

`LineStatus` (informational; `store.write_line_status` sets `counter`, `written_at`, `written_by`). A
partial update reads the file, changes the fields named and writes it back:

| Row | When | Fields set |
|---|---|---|
| After a Send | §10.11 | all: `line_id`; `send_id`; `sent_at` (`started_at`); `sender`; `product_id`; `film_id` and `film_revision`; `print_text_id` and `print_text_revision`; `lot`; `countries`; `origin_line`; `machines` = this Send's machines of the line and their result colours; `confirmed` False; `confirmed_by ""`; `stopped_needs_send` False |
| Confirmed or rejected | confirm, when `send_id` matches | `machines` colours; `confirmed`, `confirmed_by` (§11.2 step 6) |
| Stopped after a rejection | stop with reason `"rejected"`, when `send_id` matches | `stopped_needs_send` True |
| Test finished | test print end | `stopped_needs_send` True |

A Send made while the share is down writes no expected state. When the share comes back, the expected
state still describes the message before that Send, so the poller's drift check will notice the
difference (§21, coordination with states.md).

## 15. Test print

### 15.1 What it is

The test print finds where and how a film should print (plan). Stripes Administrators run it from the
film editor, on any film whatever its status, on one machine running empty film with no product. The
admin confirms the machine is running empty film and picks what to vary and the ranges; Stripes shows
the number of steps and roughly how many bags it will use (`test_bag_estimate(plan) = len(steps) ×
(bags_per_step + 1)`, one extra bag per switch). The admin types the chosen values into the film
afterwards: a normal admin edit, and the film's status doesn't change.

### 15.2 plan_test(machine_id, film_id, ranges, data, identity, *, kind="test")

1. Not `identity.is_admin(company)`: `NotAllowed(Msg(N_("Only Stripes Administrators can run test
   prints.")))`.
2. Problems (all collected, then `PlanError`): the machine exists and is active; its printer is set up;
   `print_speed` is set; the film exists (any status) and passes `validate_film`.
3. Kind `"test"`, per machine mode: `ranges.vary` must be `"position"` on encoder machines and `"delay"`
   on delay machines. Encoder machines need `head_distance_mm` (calibrated) and the film's `length_mm`.
   `ranges.step > 0` and `ranges.start <= ranges.stop`; the values are `start + i × step` for `i = 0 ..
   floor((stop − start) / step + 1e-9)`, each rounded to 0.1; positions within 0 to the film length,
   delays within 0 to 120,000 ms. `bold` and `width` values in their ranges, `resolution` values in
   `RESOLUTIONS`, at most `MAX_FONT_SAMPLES` fonts, each a valid index and size. At most
   `MAX_TEST_STEPS` steps in all.
4. Steps, in order, numbered from 1. `v0` is the film's own position (encoder) or delay (delay), or
   `ranges.start` when the film has none:
   - one step per value `v`, with the film's bold, width and resolution;
   - then one step per value of `ranges.bold` at `v0`, then per `ranges.width`, then per
     `ranges.resolution`, each changing only that setting.

   Print speed isn't varied: it is a machine setting. Every step prints the fonts of `ranges.fonts`
   side by side (`[]` means the film's font and size only).
5. Labels: each mark prints only what's being varied, which is the number the admin types into the
   film: `P{v}` on encoder machines or `D{v}` on delay machines (`v` through `_num`), then `B{bold}` when
   `ranges.bold` isn't empty, `W{width}` when `ranges.width` isn't, `R{resolution}` when
   `ranges.resolution` isn't, joined with `" | "`: `P150 | B1`, `D480 | B1`, `P152.5`.
6. `base`: a `MachinePlan` of the machine and film with no product, print text or adjustment (the
   machine + film adjustment is ignored in a test), `settings` as §6.1, `position =
   explain_position(machine, film, None, override=v0, test=True)`, `origin_line ""`.
7. `TestPlan(run_id=new_id("tp"), kind, base, steps, bags_per_step=5, timed=(company.bag_counter ==
   "none"))`.

The calibration kinds are in §16.2.

`test_step_plan(base, step)` copies `base` with the step's bold, width and resolution in `settings`,
`position = explain_position(base.machine, base.film, None, override=<the step's position or delay>,
test=True)`, `settings.position_mm` or `settings.delay_us` = `position.sent`, `test_step = step` and
the step's one-line `bag_text` (§3.5: the label, then each sample after one space).

### 15.3 A test step's message

`design_parts` of a plan with a `test_step`:

1. Item 1: CTEXT, OBJID 10001, POS `0,0`, MARG `0,0`, the film's font and size, TXT = the label.
2. Then one CTEXT per `(font, size)` of `step.fonts`, placed right of the previous item with MARG
   `f"{TEST_SAMPLE_GAP},0"`, FONTIND `font`, SIZE `size`, TXT `f"{STANDARD_FONTS[font - 1]} {size}"`
   (`Arial 3`, `Arial 4`, `Arial 5`, side by side). The names come from `hx_model.STANDARD_FONTS`; the
   machine check has already refused a printer whose list differs from `Company.standard_fonts`.
3. CPPARM as §6.1 with the step's settings. The name uses `TEST_CODE` and the step index (§7.3).

Every step is a full build, check and switch, the same as a Send, and never uses "set current print
settings", so a mark's label always matches what printed it (plan).

### 15.4 TestPrintRun.run(on_event, *, cancel=None)

Runs on `hx-test-<run_id>`; returns a `TestResult`. `counter_tag = company.bag_counter`.

1. **Lock.** `lock = store.lock(printer_id, purpose="test" (or "calibration"), run_id, line_id,
   machine_id)`. A `local_only` lock is released at once and the run ends: "A test print needs the
   server, so that nobody else uses the printer meanwhile." (`ok=False`). `LockHeld` ends it with the
   holder's message. Otherwise `start_keeping()`; the lock is held for the whole test, so no Send or
   poll interferes.
2. **Poller.** Wait as in §10.4 (no countdown).
3. **Machine check.** As §10.5 items 1 to 7, except that an ink or cartridge mismatch is only a
   `warning` event ("{machine}'s ink doesn't match the film: the marks may not show.").
   `current = load_slot(0)` gives the slot printing now and the previous message.
4. **Start.** Expected state row "Test running" (step 0). `lock.update(step=0, of=N)`. Log
   `test_start`.
5. **Step 1.** `slot = idle_slot(printing, current.slot)`; build step 1 into `slot` (§10.7), check it
   (`compare`, §10.8), then switch: PRINTMSG `slot`, PRINT 1; then one running check: `load_slot(0)`
   must show `slot` and pass `compare`, and PRIN must read 1. Then the step's start count (or, when
   timed, its start time).
6. **Each step k**, while it prints:
   1. Build ahead: build step k+1 into the other slot and check it (the idle-slot rule holds: the slot
      printing step k is never written).
   2. Count: every `second_reading_s`, `lock.check()` and the cancel token, then read `counter_tag`.
      Bags for the step = the reading minus the step's start count (a reading below the start count,
      a counter reset, becomes the new start). Timed: the step lasts `machine.seconds_per_bag ×
      bags_per_step` seconds instead, and no counter is read.
   3. The step is done at `bags_per_step` bags (or its time). Log `test_step` for it.
   4. Switch to step k+1 as in item 5 (PRINTMSG, PRINT 1, running check), take its start count, write
      the expected state's step and `lock.update(step=k+1, of=N)`, post `TestEvent(status="printing")`.
7. After the last step's bags (or its time), the end procedure (§15.5).

Stop test (the cancel token) stops after the current request and then always runs the end procedure;
`TestResult.msg` is then "Stopped by {user}." and the `test_end` reason `cancelled`.
Any failure (a refused or unanswered write, a failed check, a failed read) stops the test there and
runs the end procedure, except `LockLost`, `WrongPrinter` and `AuthFailed`: then Stripes stops talking
to that printer at once and the result says to switch printing off at the printer.

### 15.5 The end procedure

1. `client.command(Command.set("PRINT", 0))`, then `client.read_twice("PRIN")` must be 0.
2. Only when printing is confirmed off: for slot 9 and then slot 10, send `empty_message_commands(slot)`,
   then `load_slot(slot)` and `cleared(design)`. The printer has no delete command, so this overwrites
   both slots. `TestResult.cleared` is True when both are cleared. A slot that isn't: "Slot {slot} on
   {machine} couldn't be emptied."
3. The expected state's row "Test finished" (or "Test not stopped" when step 1 failed), and the line
   status row "Test finished". The machine shows "Stopped, needs a Send", Resume is blocked, and it
   needs a normal Send before production.
4. Log `test_end`. Post `TestEvent(status="cleared")` (or `"failed"`) and `TestFinished`.
5. Release the lock.

If printing couldn't be confirmed off, `TestResult.msg` is "Stripes couldn't stop printing on
{machine}. Stop it at the printer with its Print On/Off button now." and the window offers the Stop
dialog for that machine (`PrinterActions.stop`, after this run has released the lock).

### 15.6 Safety and time limits

| Limit | Value | What happens |
|---|---|---|
| No prints, counted steps | `test_no_print_s` (60 s) without the counter rising, from a step's switch or the last rise | end procedure; "No prints for {seconds} s, so the test was stopped." |
| Timed steps | the whole test time (`N × seconds_per_bag × bags_per_step`) plus `test_no_print_s`, from step 1's switch | end procedure; "The test ran past its time limit, so it was stopped." |
| Waiting for the poller | `2 × stale_s` | the run ends before any request |
| Each request | protocol.md (2 s; a Load at most about 30 s) | the step fails; end procedure |
| The test PC stops | the lock goes stale after 60 s | the poller alerts the line's notify user with sound: "Test message still printing on Bagger 3" (states.md) |

The monitor and floor board show "TEST by jsmith, step 12 of 40" from the expected state's `test` and
the lock's note (states.md).

### 15.7 Events

`TestEvent(run_id, step_index, of, status, msg, design, bags, at)`: `building` when a step's build
starts, `printing` after its switch (with the running read-back), `ok` when its bags are counted (with
`bags`), `failed`, `stopping` when the end procedure starts, `cleared` when both slots were emptied.

## 16. Calibration of encoder machines

The plan leaves the method open while the encoder is worked out. This is the interface and the safe
default built now. OPEN: everything in this section, to confirm on the test unit.

### 16.1 The procedure

Calibrating a machine happens once at installation, with the test print tool (`CalibrationWindow`,
ui.md), and starts from a rough tape measurement because a print only shows the head distance within
one bag length:

1. The admin measures the head distance roughly with a tape and saves it as
   `Calibration.rough_head_distance_mm`, choosing a film record whose length matches the empty film
   running.
2. **Distance scale.** `plan_test(kind="spacing")` and a run: two single prints, `spacing_sent_mm`
   (300 mm) apart in print position. The admin measures each mark's distance from the bag's bottom edge
   (A, then B); `spacing_gap_mm(a, b, length)` is the measured gap. If it differs from 300 mm by more
   than `SPACING_TOLERANCE_MM`, the installer corrects the wheel size on the printer: the window
   suggests `wheel_suggestion_mm(<current wheel>, 300, gap)`. Stripes never changes it (ENC is
   printer-wide and not whitelisted); the admin updates `Machine.encoder_wheel_mm` to match, and runs the
   check again. Saved: `spacing_measured_mm`, `spacing_by`, `spacing_at`.
3. **Head distance.** `plan_test(kind="head_distance")` and a run: one print at a known position. The
   admin measures where it landed from the bag's bottom edge; `head_distance_from_print(sent,
   measured, length, rough)` gives the head distance. Saved: `head_print_sent_mm`,
   `head_print_measured_mm`, `head_distance_mm`, `head_by`, `head_at`; `DataService.save` copies the
   head distance to the machine (storage.md §13.4 step 8).

Until a machine has a head distance it can't be sent to ("isn't calibrated").

### 16.2 The calibration test plans

`plan_test(..., kind="spacing" | "head_distance")` needs an encoder machine, the film's `length_mm`
(`L`) and the machine's `Calibration` with `rough_head_distance_mm` (`R`). Otherwise problems:
"Calibration is only for encoder machines.", "Measure the rough head distance first.", and for spacing
when `L < spacing_sent_mm + 50`, "This film is too short for the {mm} mm spacing check." `ranges` is
ignored; no fonts are sampled.

The base plan's machine copy gets `head_distance_mm = 0.0`, so each step's position is sent as it is
(`(v − 0 + 0) mod L = v`). Positions are chosen so the marks land near the middle of a bag if `R` is
right (`half_up` is `math.floor(x + 0.5)`):

| Kind | Steps (label, position sent) |
|---|---|
| `spacing` | `SPACE A`, `half_up((L/2 − S/2 − R) mod L)`; `SPACE B`, `half_up((L/2 + S/2 − R) mod L)`, with `S = spacing_sent_mm` |
| `head_distance` | `HEAD`, `half_up((L/2 − R) mod L)` |

Both use `bags_per_step` 5 and the film's font, size, bold, width and resolution, and run exactly like
a test print (§15.4 to §15.6) with the lock purpose `calibration`.

### 16.3 The maths

| Function | Result |
|---|---|
| `spacing_gap_mm(a, b, L)` | `(b − a) mod L` |
| `wheel_suggestion_mm(wheel, sent_gap, measured_gap)` | `round(wheel × measured_gap / sent_gap, 1)`: the printer's wheel setting is too small by the factor the gap is too long |
| `head_distance_from_print(sent, measured, L, rough)` | `base = (measured − sent) mod L`; `k = max(0, round((rough − base) / L))`; `H = base + k × L`. If `abs(H − rough) > 0.25 × L`, raise `PlanError` "The tape measurement is too far from the print to be sure. Measure the head distance again." Else return `round(H, 1)` |

The mark lands at `(sent + H) mod L` from the bag's bottom edge, the inverse of §5.1, so `H ≡ measured
− sent (mod L)`; the tape measurement picks the multiple of `L`. Examples with `L` = 610:
`head_distance_from_print(348, 150, 610, 400) == 412.0`; with rough 1000 it is 1022.0; with rough 700
it raises.

## 17. Logs

### 17.1 Rules

1. Entries go through `DataService.append_send_log(entry)` (hx-sends) or `DataService.append_log(kind,
   entry)`, which queue on this PC when the share can't be written, so no entry is lost (storage.md
   §13.6). storage.md adds the envelope (`logged_at`, `logged_by`, `logged_on`, `logged_pid`) and names
   the file after the entry's `type` and its first id (`send_id`, `run_id`, `action_id`, ...).
2. `type` is one of `LOG_TYPES`, in the log `LOG_TYPES` gives. The Send's main entry has type `send`,
   which `DataService.find_send` looks for.
3. Times inside entries (`*_at`) are by the time server's clock when a reading is at hand
   (`clock.time() + offset_s`), else this PC's: the plan logs "when, by the time server's clock".
4. `Msg` values are stored with `Msg.to_json()`, records and plan types with `hx_model.to_json`. A
   `MessageDesign` is stored with `design_to_log`: `to_json` without `preview_bmp`, plus `preview_b64`
   (the BMP in base64). Other bytes go under keys ending `_b64` (storage.md §6.2).
5. Logs are kept indefinitely; Stripes never edits or deletes one.

### 17.2 send (hx-sends): one per Send, written at the end

| Key | Value |
|---|---|
| `type` | `"send"` |
| `send_id`, `sender`, `computer` | the Send and who made it, from which PC |
| `started_at`, `finished_at` | by the time server's clock |
| `time_source`, `time_server`, `time_offset_s` | the reading (`"server"` or `"pc"`) |
| `from_local_copy`, `data_as_of` | the plan's |
| `request` | `to_json(SendRequest)` |
| `lines` | per line: `line_id`, `line_name`, `line_code`, `product` {`id`, `name`, `customer`, `revision`}, `film` {`id`, `code`, `artwork_revision`, `revision`}, `print_text` {`id`, `code`, `name`, `revision`}, `lot`, `countries`, `origin_line` |
| `machines` | per machine, in plan order: `machine_id`, `machine_name`, `machine_code`, `line_id`, `printer` {`id`, `name`, `host`, `uid`}, `placement`, `bag_text` (the exact text), `position` (`PositionWorking`: the print position and how it was worked out), `settings` (`PrintSettings`), `slot`, `message_name`, `content_code`, `commands` (each command's `xml`, in order), `expected_items` (`[[tag, [[child, value], ...]], ...]`), `expected_settings`, `clock` (the §9.5 record or None), `warnings` (Msgs), `differences` (`[[where, field, sent, read], ...]` of the failing check, or `[]`), `result` |
| `machines[i].result` | `to_json(MachineResult)` with `readbacks` as `design_to_log` dicts (the printer's pictures) and `previous_preview` as `previous_preview_b64`; it holds the print counts before and after and the result |

Who confirmed it is in its own entry (§17.4). With four pictures a machine adds about 120 KB; twelve
machines stay far below storage.md's 8 MB limit.

`result_from_log(entry)` rebuilds the `SendResult` from `send_id`, `sender`, `computer`, `started_at`,
`finished_at`, `time_source`, `from_local_copy`, `data_as_of`, each `machines[i].result` (designs back
through `design_from_log`), `log_name = entry.get("log_name", "")` and `log_queued = False`.
`request_from_log(entry)` is `from_json(SendRequest, entry["request"])`.

### 17.3 send_switch (hx-sends): per machine, right before PRINTMSG

`send_id`, `machine_id`, `line_id`, `printer_id`, `slot`, `message_name`, `content_code`, `origin_line`,
`previous_slot`, `previous_message_name`, `previous_fingerprint`, `at`. It survives a PC crash after the
switch.

### 17.4 send_confirm (hx-sends): the sender's answer

`send_id`, `sender` (from the `send` entry), `everything_right` (bool), `machine_ids`, `machines`
(`[{machine_id, message_name, fingerprint}]` as the Send left them), `at`. The envelope's `logged_by` is
who answered, always the sender.

### 17.5 send_leave (hx-sends)

`send_id`, `machine_ids`, `reread_token`, `at`. `logged_by` is who pressed Leave it.

### 17.6 stop, resume, clock_set (hx-sends) and internal_error (hx-events)

| Type | Keys |
|---|---|
| `stop` | `action_id`, `reason` (`"user"` or `"rejected"`), `send_id` (or `""`), `at`, `machines`: `[{machine_id, ok, colour, step, msg, printing_after (0, 1 or None), notes}]` |
| `resume` | `action_id`, `at`, `machines`: `[{machine_id, ok, colour, step, msg, message_name, fingerprint, origin_line, design (design_to_log or None)}]` |
| `clock_set` | `action_id`, `at`, `time_source`, `time_server`, `time_offset_s`, `machines`: `[{machine_id, ok, colour, step, msg, clock (the §9.5 record)}]` |
| `internal_error` | `where` (`"send"`, `"action"` or `"test"`), the run's `send_id`, `action_id` or `run_id`, `machine_id`, `step`, `error` (the exception's class name), `traceback` (its last 4,000 characters), `at` |

### 17.7 Test prints and calibration (hx-admin)

The plan puts test prints in the admin-change log, every step included (machine, film, settings, print
counts).

| Type | Keys |
|---|---|
| `test_start` | `run_id`, `kind`, `machine_id`, `machine_name`, `printer_id`, `film_id`, `film_code`, `film_revision`, `ranges` (`TestRanges`), `steps` (`[TestStep]`), `bags_per_step`, `timed`, `counter_tag`, `previous` {`slot`, `name`, `fingerprint`}, `at` |
| `test_step` | `run_id`, `kind`, `machine_id`, `film_id`, `index`, `of`, `label`, `slot`, `message_name`, `content_code`, `position` (`PositionWorking`), `settings` (`PrintSettings`), `fonts`, `counter_tag`, `count_start`, `count_end`, `bags`, `started_at`, `ended_at`, `running` (the step's running read-back, `design_to_log`) |
| `test_end` | `run_id`, `kind`, `machine_id`, `film_id`, `steps_done`, `of`, `ok`, `reason` (`"done"`, `"cancelled"`, `"no_prints"`, `"time_limit"` or `"failed"`), `msg`, `printing_off`, `cleared`, `slots` {`"9"`: bool, `"10"`: bool}, `at` |

The calibration results themselves are saved as the `Calibration` record (`record_saved`, storage.md).

## 18. Events

| Event | Posted | Statuses and content |
|---|---|---|
| `SendEvent(machine_id="")` | whole Send | `check_choice` running/ok; `lock` running (its `at` starts the countdown) and ok ("go"); `set_clock` warning (PC time) |
| `SendEvent(machine_id=...)` | per machine and step | `running` at the start of each step it reaches; then `ok`, `failed` (with `msg`), `skipped` (wait_poller with a `local_only` lock; every step after a cancel) or `warning` (`lock` with `local_only`; `set_clock` with PC time). `design` is set on check_machine (the message printing before), verify_idle, confirm_1 and confirm_2. After confirm_2: `operator_confirm` running |
| `SendFinished(result)` | once, last | |
| `ActionEvent` | per machine of a Stop, Resume or Set clocks | `running`, then `ok`, `failed` or `warning`; `design` on Resume's read |
| `ActionFinished(result)` | once, last | |
| `TestEvent`, `TestFinished` | §15.7 | |

Events are posted from the worker threads through `on_event` and never carry Tk objects; pictures
travel as BMP bytes inside `MessageDesign`.

## 19. Messages

Each module defines `ES` with its own texts and calls `i18n.add_spanish(ES)` (architecture §10.6).
`{machine}`, `{line}`, `{film}`, `{print_text}`, `{printer}`, `{name}`, `{other}` and `{machines}` are
record names; `{user}` and `{sender}` are Windows user names; `{country}` and `{words}` are names from
Stripes' own lists; `{label}` is an administrator's question text. None is text a sender typed. A
`{where}` value is one of the nested Msgs listed. ui.md uses these texts through the `Msg`s and doesn't
redefine them.

### 19.1 hx_message

| English | Spanish |
|---|---|
| Only Stripes MapleJet Senders can send. | Solo los miembros de Stripes MapleJet Senders pueden enviar. |
| Only Stripes Administrators can run test prints. | Solo los miembros de Stripes Administrators pueden hacer impresiones de prueba. |
| Choose at least one line. | Elija al menos una línea. |
| {line} is in this Send twice. | {line} está dos veces en este envío. |
| A line in this Send no longer exists or isn't active. | Una línea de este envío ya no existe o no está activa. |
| Tick at least one machine on {line}. | Marque al menos una máquina en {line}. |
| {machine} isn't an active machine on {line}. | {machine} no es una máquina activa de {line}. |
| {machine} is in this Send twice. | {machine} está dos veces en este envío. |
| {machine} and {other} use the same printer. | {machine} y {other} usan la misma impresora. |
| Choose a product for {line}. | Elija un producto para {line}. |
| The product chosen for {line} isn't active. | El producto elegido para {line} no está activo. |
| Choose a film for {line}. | Elija una película para {line}. |
| Film {film} can't be sent to {line}: it must be active, belong to the product and use the line's cartridge type. | La película {film} no se puede enviar a {line}: debe estar activa, ser del producto y usar el tipo de cartucho de la línea. |
| Choose a print text for {line}. | Elija un texto de impresión para {line}. |
| Print text {print_text} can't be used with film {film}. | El texto de impresión {print_text} no se puede usar con la película {film}. |
| Enter the lot for {line}. | Escriba el lote para {line}. |
| The lot for {line} must be {min} to {max} digits. | El lote para {line} debe tener de {min} a {max} dígitos. |
| Choose at least one country of origin for {line}. | Elija al menos un país de origen para {line}. |
| {country} isn't on the country list. | {country} no está en la lista de países. |
| {country} is chosen twice. | {country} está elegido dos veces. |
| Fill in "{label}" for {line}. | Complete "{label}" para {line}. |
| "{label}" can be at most {n} characters. | "{label}" puede tener como máximo {n} caracteres. |
| {where} contains words about origin: {words}. Origin goes only in the origin line. | {where} contiene palabras de origen: {words}. El origen va solo en la línea de origen. |
| {where} contains characters Stripes can't send to a printer: {codes}. | {where} contiene caracteres que Stripes no puede enviar a una impresora: {codes}. |
| {where} is too long: at most {n} characters. | {where} es demasiado largo: como máximo {n} caracteres. |
| The text before item {n} | El texto antes del elemento {n} |
| The text after item {n} | El texto después del elemento {n} |
| The fixed text of item {n} | El texto fijo del elemento {n} |
| The free text "{label}" | El texto libre "{label}" |
| The code of {name} | El código de {name} |
| The pre-printed origin | El origen preimpreso |
| Country {name} | País {name} |
| Item {n} | Elemento {n} |
| The printer of {machine} isn't set up: it is missing, not active or has no saved password. | La impresora de {machine} no está configurada: falta, no está activa o no tiene contraseña guardada. |
| {machine} has no print speed. An administrator must set it. | {machine} no tiene velocidad de impresión. Un administrador debe configurarla. |
| {machine} isn't calibrated: it has no head distance. | {machine} no está calibrada: no tiene distancia del cabezal. |
| Film {film} has no length, which encoder machines need. | La película {film} no tiene largo, y las máquinas con encoder lo necesitan. |
| Film {film} has no print position, which encoder machines need. | La película {film} no tiene posición de impresión, y las máquinas con encoder la necesitan. |
| Film {film} has no delay, which delay machines need. | La película {film} no tiene retardo, y las máquinas por retardo lo necesitan. |
| The delay works out at {ms} ms, outside 0 to 120000 ms. | El retardo resulta en {ms} ms, fuera del rango de 0 a 120000 ms. |
| The print would still be running when the film stops ({end} of {length} mm), so the next bag would go out blank. | La impresión seguiría en curso cuando la película se detenga ({end} de {length} mm), así que la siguiente bolsa saldría en blanco. |
| Print length unknown: Stripes can't check that the print ends before the film stops. | Largo de impresión desconocido: Stripes no puede comprobar que la impresión termine antes de que la película se detenga. |
| The name must be 1 to {n} characters. | El nombre debe tener de 1 a {n} caracteres. |
| The status isn't valid. | El estado no es válido. |
| The print text code must be 1 to 3 capital letters or digits. | El código del texto de impresión debe tener de 1 a 3 letras mayúsculas o dígitos. |
| TST is kept for test prints. | TST está reservado para las impresiones de prueba. |
| The print text has no items. | El texto de impresión no tiene elementos. |
| A print text can have at most {n} items. | Un texto de impresión puede tener como máximo {n} elementos. |
| Item ids must be unique. | Los identificadores de los elementos deben ser únicos. |
| Item {n} has an unknown kind. | El elemento {n} es de un tipo desconocido. |
| A print text can have only one origin item. | Un texto de impresión solo puede tener un elemento de origen. |
| Item {n} must be placed right of or below the previous item. | El elemento {n} debe ir a la derecha o debajo del anterior. |
| The gap before item {n} must be 0 to {max}. | El espacio antes del elemento {n} debe ser de 0 a {max}. |
| The origin item can't have text before or after it. | El elemento de origen no puede tener texto antes ni después. |
| Item {n} needs its fixed text. | El elemento {n} necesita su texto fijo. |
| Item {n} needs a date format. | El elemento {n} necesita un formato de fecha. |
| The packed-on date can't have an offset. | La fecha de empaque no puede tener desfase. |
| The best-by offset of item {n} must be 1 to 999 days. | El desfase de consumo preferente del elemento {n} debe ser de 1 a 999 días. |
| The best-by offset of item {n} must be 1 to 999 months. | El desfase de consumo preferente del elemento {n} debe ser de 1 a 999 meses. |
| The best-by offset of item {n} must be 1 to 99 years. | El desfase de consumo preferente del elemento {n} debe ser de 1 a 99 años. |
| Item {n} needs a question for the sender. | El elemento {n} necesita una pregunta para el remitente. |
| The free text of item {n} must allow 1 to {max} characters. | El texto libre del elemento {n} debe permitir de 1 a {max} caracteres. |
| The film code must be 1 to 6 capital letters or digits. | El código de la película debe tener de 1 a 6 letras mayúsculas o dígitos. |
| Another film already uses the code {code}. | Otra película ya usa el código {code}. |
| The film's product doesn't exist. | El producto de la película no existe. |
| The side, cartridge type or ink colour isn't valid. | El lado, el tipo de cartucho o el color de tinta no es válido. |
| The film length must be more than 0 and at most {max} mm. | El largo de la película debe ser mayor que 0 y como máximo {max} mm. |
| The print position must be between 0 and the film length. | La posición de impresión debe estar entre 0 y el largo de la película. |
| The delay must be 0 to 120000 ms. | El retardo debe ser de 0 a 120000 ms. |
| The print length must be more than 0 and at most the film length. | El largo de impresión debe ser mayor que 0 y como máximo el largo de la película. |
| Font, size, resolution, bold or width is out of range. | La fuente, el tamaño, la resolución, la negrita o el ancho están fuera de rango. |
| Enter the pre-printed origin. | Escriba el origen preimpreso. |
| A print text allowed with this film doesn't exist. | Un texto de impresión permitido con esta película no existe. |
| Print text {print_text} has an origin item, but this film's origin is pre-printed. | El texto de impresión {print_text} tiene un elemento de origen, pero el origen de esta película está preimpreso. |
| Print text {print_text} has no origin item, but this film needs one. | El texto de impresión {print_text} no tiene elemento de origen, pero esta película lo necesita. |
| An active film needs at least one print text and a default print text. | Una película activa necesita al menos un texto de impresión y uno predeterminado. |
| The default print text must be one of the film's print texts. | El texto de impresión predeterminado debe ser uno de los de la película. |
| The code must be 1 to 3 capital letters or digits. | El código debe tener de 1 a 3 letras mayúsculas o dígitos. |
| The cartridge type isn't valid. | El tipo de cartucho no es válido. |
| The machine's line doesn't exist. | La línea de la máquina no existe. |
| The machine's printer doesn't exist. | La impresora de la máquina no existe. |
| {printer} is already used by {machine}. | {printer} ya la usa {machine}. |
| The placement must be Encoder or Delay. | La colocación debe ser Encoder o Retardo. |
| The print speed must be 1 to 2500. | La velocidad de impresión debe ser de 1 a 2500. |
| The seconds per bag must be 0.5 to 60. | Los segundos por bolsa deben ser de 0,5 a 60. |
| The head distance must be more than 0 and at most {max} mm. | La distancia del cabezal debe ser mayor que 0 y como máximo {max} mm. |
| The encoder wheel must be 20 to 150 mm. | La rueda del encoder debe medir de 20 a 150 mm. |
| The pulses per turn must be 1 to 1000000. | Los pulsos por vuelta deben ser de 1 a 1000000. |
| Country names must be unique, not empty and at most {n} characters. | Los nombres de país deben ser únicos, no estar vacíos y tener como máximo {n} caracteres. |
| {name} is used by more than one country. | {name} lo usa más de un país. |
| Ink names must be unique, each with a cartridge type and a colour. | Los nombres de tinta deben ser únicos, cada uno con tipo de cartucho y color. |
| The lot rules must allow 1 to 20 digits, the minimum no more than the maximum. | Las reglas de lote deben permitir de 1 a 20 dígitos, con el mínimo no mayor que el máximo. |
| The free-text limit must be 1 to 100 characters. | El límite de texto libre debe ser de 1 a 100 caracteres. |
| The standard font list must have 6 fonts. | La lista de fuentes estándar debe tener 6 fuentes. |
| The bag counter must be PMCNT, MSCNT or none. | El contador de bolsas debe ser PMCNT, MSCNT o none. |
| Print text {name} now contains words about origin: {words}. | El texto de impresión {name} ahora contiene palabras de origen: {words}. |
| The code of {name} now contains words about origin: {words}. | El código de {name} ahora contiene palabras de origen: {words}. |
| The test must vary the print position on encoder machines and the delay on delay machines. | La prueba debe variar la posición de impresión en máquinas con encoder y el retardo en máquinas por retardo. |
| The test range isn't valid: check the start, stop and step. | El rango de la prueba no es válido: revise el inicio, el final y el paso. |
| The test would have {n} steps; at most {max} are allowed. | La prueba tendría {n} pasos; se permiten como máximo {max}. |
| A tested value is out of range. | Un valor de la prueba está fuera de rango. |
| At most {n} fonts can be printed side by side. | Se pueden imprimir como máximo {n} fuentes lado a lado. |
| Calibration is only for encoder machines. | La calibración es solo para máquinas con encoder. |
| Measure the rough head distance first. | Mida primero la distancia aproximada del cabezal. |
| This film is too short for the {mm} mm spacing check. | Esta película es demasiado corta para la prueba de separación de {mm} mm. |
| The tape measurement is too far from the print to be sure. Measure the head distance again. | La medida con cinta está demasiado lejos de la impresión para estar seguros. Vuelva a medir la distancia del cabezal. |
| No Send is recorded for {machine}. Send instead. | No hay ningún envío registrado para {machine}. Haga un envío. |
| {machine} is in the middle of a Send or a test. | {machine} está en medio de un envío o de una prueba. |
| The last Send to {machine} was rejected. Send again instead. | El último envío a {machine} fue rechazado. Haga un envío nuevo. |
| {machine} needs a Send after the test print. | {machine} necesita un envío después de la impresión de prueba. |
| {machine} needs a new Send before it can print again. | {machine} necesita un envío nuevo antes de volver a imprimir. |
| The last Send to {machine} wasn't confirmed. Send again instead. | El último envío a {machine} no se confirmó. Haga un envío nuevo. |
| The message on {machine} isn't the last confirmed Send. Send again instead. | El mensaje en {machine} no es el último envío confirmado. Haga un envío nuevo. |

`STEP_TEXTS`:

| Key | English | Spanish |
|---|---|---|
| `check_choice` | Checking the sender and the choice | Revisando el remitente y la selección |
| `lock` | Counting down and locking the printer | Cuenta atrás y bloqueo de la impresora |
| `wait_poller` | Waiting for the monitoring PC to step aside | Esperando a que la PC de monitoreo se aparte |
| `check_machine` | Checking the machine | Revisando la máquina |
| `set_clock` | Setting the clock | Ajustando el reloj |
| `build` | Building the new message | Creando el mensaje nuevo |
| `verify_idle` | Checking the new message before it goes live | Revisando el mensaje nuevo antes de activarlo |
| `switch` | Switching to the new message | Cambiando al mensaje nuevo |
| `confirm_1` | First check of the running message | Primera revisión del mensaje en impresión |
| `confirm_2` | Second check of the running message | Segunda revisión del mensaje en impresión |
| `operator_confirm` | Waiting for the sender to confirm | Esperando la confirmación del remitente |
| `stop` | Turning printing off | Desactivando la impresión |
| `resume` | Turning printing on | Activando la impresión |

`NOTE_TEXTS`:

| Note | English | Spanish |
|---|---|---|
| `pc_time` | The time server didn't answer, so the printer clock was set from this PC's clock. | El servidor de hora no respondió, así que el reloj de la impresora se ajustó con el reloj de esta PC. |
| `no_lock` | Sent and confirmed by the printer, but Stripes couldn't check whether anyone else was sending to this line. Check the first bags on each machine. | Enviado y confirmado por la impresora, pero Stripes no pudo comprobar si alguien más estaba enviando a esta línea. Revise las primeras bolsas de cada máquina. |
| `local_copy` | Sent from this PC's copy of the templates, because the server couldn't be reached. | Enviado con la copia de las plantillas de esta PC, porque no se pudo conectar con el servidor. |
| `ink_mismatch` | The printer's ink doesn't suit this film. | La tinta de la impresora no es adecuada para esta película. |
| `film_stop` | The print may still be running when the film stops. | Puede que la impresión siga en curso cuando la película se detenga. |
| `cancelled` | Cancelled. Nothing was sent. | Cancelado. No se envió nada. |

### 19.2 hx_time

| English | Spanish |
|---|---|
| {server} didn't answer the time request. | {server} no respondió a la solicitud de hora. |
| {server} sent a time answer that doesn't make sense. | {server} envió una respuesta de hora que no tiene sentido. |

### 19.3 hx_send

| English | Spanish |
|---|---|
| The templates changed since you checked them. Look at the Send window again. | Las plantillas cambiaron desde que las revisó. Vuelva a mirar la ventana de envío. |
| {line} is being sent by {user}. | {user} está enviando a {line}. |
| {machine} is running a test print by {user}. | {user} está haciendo una impresión de prueba en {machine}. |
| {machine} is being changed by {user}. | {user} está haciendo cambios en {machine}. |
| Waited {seconds} s for the PC polling {machine} to step aside. Try again. | Se esperó {seconds} s a que la PC que monitorea {machine} se apartara. Inténtelo de nuevo. |
| {machine}'s encoder settings changed: wheel {wheel} mm, {ppr} pulses per turn. Every print would move. Ask an administrator. | La configuración del encoder de {machine} cambió: rueda de {wheel} mm, {ppr} pulsos por vuelta. Todas las impresiones se moverían. Avise a un administrador. |
| {machine}'s printer has fonts that differ from Stripes' standard list. | La impresora de {machine} tiene fuentes distintas de la lista estándar de Stripes. |
| {machine} reports no ink. | {machine} no informa ninguna tinta. |
| {machine} reports ink "{ink}", which isn't on Stripes' ink list. | {machine} informa la tinta "{ink}", que no está en la lista de tintas de Stripes. |
| {machine} is set for black ink; this film needs white. | {machine} está configurada para tinta negra; esta película necesita tinta blanca. |
| {machine} is set for white ink; this film needs black. | {machine} está configurada para tinta blanca; esta película necesita tinta negra. |
| {machine} is set for paper cartridges; this film needs plastic. | {machine} está configurada para cartuchos de papel; esta película necesita plástico. |
| {machine} is set for plastic cartridges; this film needs paper. | {machine} está configurada para cartuchos de plástico; esta película necesita papel. |
| {machine}'s clock reads {printer_time} after it was set to {expected}. | El reloj de {machine} marca {printer_time} después de ajustarlo a {expected}. |
| The message read back from {machine} differs from what was sent ({n} differences). | El mensaje leído de {machine} difiere de lo enviado ({n} diferencias). |
| The date {machine} would print differs from the date Stripes expects. | La fecha que imprimiría {machine} difiere de la fecha que espera Stripes. |
| {machine} is printing slot {got}, not slot {slot}. | {machine} está imprimiendo la ranura {got}, no la ranura {slot}. |
| {machine} didn't turn printing on. | {machine} no activó la impresión. |
| Stripes had an internal error at this step: {error} | Stripes tuvo un error interno en este paso: {error} |
| This machine may be printing the new message, the old one, or nothing. Check the bags now. | Esta máquina puede estar imprimiendo el mensaje nuevo, el anterior o nada. Revise las bolsas ahora. |
| Stripes can't find this Send yet. Try again in a minute. | Stripes todavía no encuentra este envío. Inténtelo de nuevo en un minuto. |
| Only {sender} can confirm this Send. | Solo {sender} puede confirmar este envío. |
| Your answer was logged, but Stripes couldn't update the monitor for {machines}. | Su respuesta quedó registrada, pero Stripes no pudo actualizar el monitor de {machines}. |
| {user} said something is wrong with this Send. | {user} indicó que algo está mal en este envío. |
| Only Stripes MapleJet Senders can stop, resume or set clocks. | Solo los miembros de Stripes MapleJet Senders pueden detener, reanudar o ajustar relojes. |
| Stripes couldn't stop printing on {machine}. Stop it at the printer with its Print On/Off button now. | Stripes no pudo detener la impresión en {machine}. Deténgala ahora en la impresora con su botón Print On/Off. |
| Stripes can't reach the server, so it can't check the last confirmed Send. | Stripes no puede conectarse con el servidor, así que no puede revisar el último envío confirmado. |
| A test print needs the server, so that nobody else uses the printer meanwhile. | Una impresión de prueba necesita el servidor, para que nadie más use la impresora mientras tanto. |
| {machine}'s ink doesn't match the film: the marks may not show. | La tinta de {machine} no coincide con la película: puede que las marcas no se vean. |
| No prints for {seconds} s, so the test was stopped. | No hubo impresiones durante {seconds} s, así que se detuvo la prueba. |
| The test ran past its time limit, so it was stopped. | La prueba superó su tiempo límite, así que se detuvo. |
| Stopped by {user}. | Detenida por {user}. |
| Slot {slot} on {machine} couldn't be emptied. | No se pudo vaciar la ranura {slot} de {machine}. |

## 20. Tests

`unittest`, in the style of the existing test files (architecture §9). Pure tests build records and
`HxData` by hand or from a `FakeWorld`'s store. Threaded tests use a `FakeWorld` with a fresh
`GateRegistry()`, `SystemClock` and `TIMINGS.scaled(0.02)`, and identities from `world.identity(...)`
(`jsmith` a sender, `akim` a second sender, `admin` an administrator). Every test that drives a fake ends
with `assertEqual(fake.violations, [])` and `assertEqual(fake.max_in_flight, 1)` unless it provokes one on
purpose. No test waits more than about 0.5 s of real time.

### 20.1 test_hx_message.py

| Class | Test | Asserts |
|---|---|---|
| OriginTests | phrase | the §2.2 table; `[]` raises `ValueError` |
| | words | the §2.3 table, with a `Company` holding the `FakeWorld` countries; an inactive country still counts |
| | characters | `bag_text_problems("A<B>", where)` is one Msg whose `codes` is `"U+003C, U+003E"`; `"ñ°é&"` passes; a tab and `"€"` are refused |
| | change text | the §2.5 examples |
| BuilderTests | date formats | `render_date` for every `DATE_FORMATS` key on 29 Oct 2026 gives the §3.2 column; 31 Jan 2027 plus 1 month is 28 Feb 2027, 31 Jan 2028 plus 1 month is 29 Feb 2028; 29 Feb 2028 plus 1 year is 28 Feb 2029; 10 Apr 2022 with years 1, months 3, days 5 and FRMT `Y/M/D` is `2023/07/15`; FRMT `j` raises `ValueError` |
| | placement | items right, below, right, below give OBJID 10001 to 10005 and the XRID, YRID, SXAS, SYAS and MARG of §3.4 (the fourth item lines up with item 2, the fifth with item 4's line start); an optional empty free text is left out and the next item refers to the one before it |
| | print text rules | one failing case per rule of §3.6, each giving its message; the `FakeWorld` print text has no problems; code `TST` is refused |
| | bag text | the two §3.5 examples |
| ValidationTests | merge | `FakeWorld` data: every field of §4.4's table; each §4.3 problem in turn gives `PlanError` with that message among `problems`; not a sender raises `NotAllowed`; a pre-printed film gives `origin_line` `USA (pre-printed)` and empty countries; merging twice gives equal plans |
| | offers | `films_for_line` leaves out inactive films, other products and the other cartridge type; `print_texts_for_film` keeps the film's order and the origin rule |
| | conflicts | adding the alias `LOT` to a country makes `origin_conflicts` name the print text whose lot item has `Lot ` before it, and `merge_plan` then refuses it |
| | records | `validate_company`, `validate_line`, `validate_machine`, `validate_film`: one failing case per rule; a machine coded `US` is refused |
| PositionTests | encoder | the §5.1 table, `wrapped` included; texts `(150 - 412 + 0) mod 610 = 348 mm` and `(150 - 412 - 3) mod 610 = 345 mm` |
| | delay | §5.2's examples; text `480 + 20 = 500 ms = 500000 µs`; `test=True` ignores the adjustment and ends with ` (test print: adjustment ignored)` |
| | film stop | `film_stop_warning(348, 300, 610)` has `end` `648` and `length` `610`; `(348, 200, 610)` and `(348, None, 610)` are None; `merge_plan` adds "Print length unknown" for an encoder machine whose film has none |
| BuildTests | golden | `build_commands` for the `FakeWorld` delay and encoder machines equals §6.4 exactly; `expected_settings` has 23 keys with DELAY `480000`, DLYMM `0`, RESNAM `UltraFine`, ANG `30` and no RESO; `PRINTER_DEFAULT_SETTINGS == hx_fake.FAKE_DEFAULT_SETTINGS` |
| | names | §7.6; `canonical_design` of the golden example is 679 bytes with the §7.1 sha256; 200 random valid part combinations each give a name of at most 20 characters that parses back to its parts; the golden name with any of the other 23 check letters, or with its slot character swapped, parses to None |
| | empty and idle | `empty_message_commands(10)` gives `<CRTMSG></CRTMSG>` and `<SAVEMSG>10</SAVEMSG>`; `idle_slot` gives 10 for slot 9, 9 for slot 10, 9 for slot 1 and for None, printing or not |
| | clock | the §9.4 table; a year after 2050 raises |
| CompareTests | match | a `MessageDesign` shaped like the fake's verbose render of the golden example (SIZE one lower, defaults, BOUND, LANG, OBJTYP) gives `[]` |
| | rules | each gives exactly one difference: SIZE read as sent instead of one lower; TXT with a trailing space; an item field `SLANT`; WCOEFF `1`; PRNSPD missing; ANG `31`; an extra setting `FOO`; `black_pixels` 0; a wrong name; a wrong slot. An empty slot gives one difference. A different RESO, BOUND or LANG gives none |
| | dates | read at 29 Oct 2026 12:00 local, TXT `10/29/26` passes and `10/28/26` fails; read at 30 Oct 00:01:00, `10/29/26` passes; read at 30 Oct 00:03:00 it fails |
| | fingerprint | the slot 9 fixture with CRLF and with LF gives the same fingerprint; changing a CTDAT TXT, a BOUND, a relative item's POS, the slot, `read_at` or the picture doesn't change it; changing a CTEXT TXT, a SIZE or a setting does; `origin_from_design` of the golden message is `Product of USA and Canada` |
| | cleared | an empty-slot read-back and a design with no items and no name are cleared; the golden message isn't |
| ResumeTests | rules | one case per row of §13.3's table; `resume_mismatch` for a changed name, slot or fingerprint |
| TestPlanTests | steps | encoder, film position 150, range 140 to 160 step 5, bold [1, 2]: 7 steps labelled `P140 \| B1`, `P145 \| B1`, `P150 \| B1`, `P155 \| B1`, `P160 \| B1`, `P150 \| B1`, `P150 \| B2`; delay labels start with `D`; a vary/placement mismatch, a range giving 101 steps and a bold of 11 are refused; `test_bag_estimate` of 7 steps is 42 |
| | step plan | `test_step_plan` of `P150` sends DLYMM 348 with the adjustment ignored; fonts `[(2, 3), (2, 4)]` give the label item, then `Arial 3` and `Arial 4` placed right with MARG `8,0`; the name uses `TST` and the step index |
| | calibration | film length 610, rough 400, spacing 300: steps `SPACE A` at 365 and `SPACE B` at 55, `HEAD` at 515, each sent as is; `spacing_gap_mm(167, 467, 610) == 300`; `wheel_suggestion_mm(56, 300, 306) == 57.1`; the §16.3 head-distance examples, including `(515, 317, 610, 400) == 412.0` |

### 20.2 test_hx_time.py

| Class | Test | Asserts |
|---|---|---|
| SntpTests | fake server | `FakeSntp(offset_s=3600)`: `sntp_query("127.0.0.1", port=..., timeout_s=0.5)` is within 0.5 s of `time.time() + 3600` |
| | silent and bad | a silent `FakeSntp` raises `TimeServerError` after about 0.2 s with `timeout_s=0.2`; a small UDP server in the test answering with mode 3, a wrong originate timestamp, stratum 0 or leap indicator 3 raises it each time |
| TimeSourceTests | hosts | a `query` stub where the first host raises and the second answers: `source "server"`, `server` the second host, `offset_s` the answer minus `clock.time()`; the stub receives `port` and `timeout_s` keywords; `parse_host` of `time.example.local`, `192.0.2.123` and `127.0.0.1:4123` |
| | cache | `FakeClock`: a second `now()` within `cache_s` makes no query; after `advance(600)` it queries; `fresh=True` always queries; no hosts or every host failing gives `source "pc"`, `offset_s 0.0`, and clears the cache; `now_from` keeps the offset and moves with the clock |
| | local | `to_local(1793275200.0, offset_fn=lambda u: -14400.0)` is 29 Oct 2026 08:00 |
| DstTests | change | §9.3 item 4 with `FakeClock`; a constant offset never fires; `previous_s` and `offset_s` after the change |

### 20.3 test_hx_send.py

| Class | Test | Asserts |
|---|---|---|
| SendTests | delay | one `FakeWorld` delay printer, countries USA and Canada, lot 4417: one AMBER, ok result; the fake's `writes` are exactly `TD, CRTMSG, CTDAT, CTEXT, CTEXT, CPPARM, SAVEMSG, PRINTMSG, PRINT`; the design's Autopilot bodies equal §6.4; three read-backs (slot 9, then slot 0 twice) after the first slot 0 Load; the fake prints slot 9; `print_bags(1)` records `delay_us` 480000; the expected state is `running`, AMBER, unconfirmed, with the fingerprint of the fake's slot 9; the line status is written; `find_send` returns the `send` entry with four pictures; a `send_switch` entry exists; the events per machine follow the step order |
| | encoder | the encoder fake: DLYMM 348, `printed[-1]["position_mm"] == 348`, ENWHD and ENPPR read |
| | alternation | a second Send builds in slot 10 and switches to it; a third in slot 9 |
| | partial | two machines, one fake answering CTEXT with `3`: that machine red at build, still printing its previous message, the other live; the line status holds both colours; `failure_rows` has one row |
| | each failure | the injection table below: the step, `after_switch`, the expected-state row, and that nothing was written after the failure |
| | lock held | another store holds the printer's lock (purpose send, `EXAMPLE\akim`): the line's machines red at lock with "Line 1 is being sent by EXAMPLE\akim."; that fake received no request |
| | poller | a second store keeps writing the machine's `poll.json` with a rising counter and no `paused_for_lock`: the Send waits; once it writes the lock's token, the Send carries on; if it never does, the machine is red at wait_poller after `2 × stale_s` with no request sent |
| | share down | `fs.down = True` with the local copy loaded: AMBER with notes `no_lock` and `local_copy`, no expected state written, `log_queued` True |
| | PC time | a silent `FakeSntp`: AMBER, note `pc_time`, the warning event, and the TD still sent from the PC's time |
| | cancel | cancel during the countdown: no request reached the fake, no lock file is left, no log written, `is_cancelled(result)` |
| | templates changed | the film's position changed in `data.current` after `merge_plan`: `run` raises `PlanError` with "The templates changed ..." and nothing is written |
| | not a sender | `NotAllowed`, nothing written |
| | ink | INKT `Example White` with a black film: red at check_machine, note `ink_mismatch`, message "Line 1 Bagger 1 is set for white ink; this film needs black." |
| | fonts | the fake's font list with one name changed: red at check_machine |
| | clock | a callable `override("TIME", ...)` answering a time 5 minutes ahead of the fake's clock: red at set_clock with the clock message, after one re-read |
| | dates | a `web_reply_hook` changing the CTDAT TXT of the slot 9 Load: red at verify_idle with "The date ... differs ..." |
| ConfirmTests | right | the sender confirms: the expected state is confirmed and GREEN; a `send_confirm` entry; the line status confirmed |
| | someone else | `akim` confirming `jsmith`'s Send: `NotAllowed`; nothing written |
| | wrong, then stop | `confirm(..., False)` then `stop(reason="rejected")`: PRINT 0 sent and PRIN read twice; the rows Rejected and Stopped; `stopped_needs_send`; `check_resume` refuses with the rejected message; `resume` refuses with no write to the fake |
| | superseded | confirming an older Send after a newer one: no expected-state change; the log entry is still written |
| | pending | `pending_confirmation` lists the live machines, and `[]` after the confirmation |
| FailureTests | rows | `failure_rows` after a verify_idle failure holds the hooked difference; after an after-switch failure it has `after_switch_text` and the previous picture |
| | try again | `plan.restricted([failed])` runs with a new `send_id`, only that machine, from the lock step |
| | leave it | `leave_it`: `reread_token`, `left_by` and `left_at` set, still RED with its failure, a `send_leave` entry |
| ActionTests | stop and resume | a confirmed Send, `stop(reason="user")`, then `resume`: PRINT 1, PRIN read twice, mode `running`, GREEN |
| | resume refused | after `stop`, the test saves another message into the printing slot directly on the fake: `check_resume` and `resume` refuse with the mismatch message |
| | stop fails | `override("PRINT", FakePrinter.DROP)`: red with the "Stop it at the printer" message, mode `stop_requested_at_printer` |
| | set clocks | `clock_offset_s = 7860`: afterwards the printer clock is within 90 s; a `clock_set` entry with `before` and `after`; a `pc_time` note on the expected state is cleared |
| TestPrintTests | counted | 3 steps with a PMCNT override rising by 5 per read: slots alternate, each step's label is on the fake's slot, three `test_step` entries, then PRINT 0, both slots cleared, `TestResult.cleared`, the row Test finished with Resume blocked, `stopped_needs_send` |
| | no prints | the counter never rises: the end procedure after `test_no_print_s`, reason `no_prints` |
| | timed | `bag_counter "none"`: no counter is read; each step lasts `seconds_per_bag × 5` |
| | stop test | cancel after step 1: the end procedure still runs |
| | needs the server | the share down: refused before any request |
| | calibration | kind `spacing`, length 610, rough 400: DLYMM 365 then 55 |
| SafetyTests | whitelist | across the whole file: every fake's `writes` within `ALLOWED_WRITE_TAGS`; never SETPPARAM; never "SAVEMSG to the printing slot" |

Failure injections ("each failure"):

| Injection | Fails at | After the switch | Expected-state row |
|---|---|---|---|
| `override("HXUI", "1110")` | check_machine | no | Failed before the switch |
| `files["/sfl.xml"] = None` | check_machine | no | Failed before the switch |
| `override("TD", "3")` | set_clock | no | Failed before the switch |
| `override("CPPARM", "3")` | build | no | Failed before the switch |
| `web_reply_hook` changing the CTEXT TXT of the slot 9 Load | verify_idle | no | Failed before the switch |
| `override("PRINTMSG", FakePrinter.DROP)` | switch | yes | Failed after the switch (`switch_pending`) |
| PRIN answering `0` after PRINT 1 (a callable override) | confirm_1 | yes | Failed after the switch |
| `web_reply_hook` changing the CTEXT TXT of the second slot 0 Load | confirm_2 | yes | Failed after the switch |

## 21. OPEN items

| Item | Safe default built now | To confirm on the test unit |
|---|---|---|
| Encoder calibration (§16) | the spacing and head-distance runs and maths; an encoder machine without a head distance can't be sent to; Stripes never changes the wheel size | the whole method, the spacing tolerance (2 mm), whether a print lands where calculated |
| Which counter counts bags | `Company.bag_counter` `PMCNT`; a step's count restarts at each switch, so an MSCNT reset doesn't matter; `none` makes steps timed | which counter, and whether it resets on a message change |
| Print speed units and range | `Machine.print_speed` required, 1 to 2500, sent as PRNSPD and compared exactly | cm/min; whether 2400 is accepted and read back; whether it matters with the encoder |
| Film print length (film-stop warning) | no warning without `Film.print_length_mm`, and "Print length unknown" shown | how it is measured; what happens when a print runs past the stop |
| Item positions and gaps | first item at POS `0,0`, others relative, gaps in canvas units 0 to 50 | units, the need for an edge margin, how relative items read back |
| `&`, `<`, `>` and `ñ` | `<` and `>` refused; `&` escaped when sent, compared raw | how they print and read back |
| TD hour and rollover | HOUR 0 to 23, rollover DTRH 0, DTRM 0, always sent | the accepted values; that midnight is the rollover; what a TD without the rollover does |
| TD seconds | rounded to the nearest minute; 90 s tolerance on the read-back | whether the printer zeroes its seconds |
| Best-by month arithmetic | the day cut to the month's end; any difference fails the date check | the printer's rule at month ends and on 29 Feb |
| RESNAM spellings | `UltraFine`, `Fine`, `Normal`, `Draft` | the names for 1 to 3 |
| DELAY in the read-back | µs, compared exactly | that 480000 reads back as 480000 |
| Empty messages | CRTMSG with no name, SAVEMSG with no items | that the printer accepts and reads them back empty |
| Load of slot 0 with printing off | its slot is taken as the selected message | what it returns |
| Ink name | a settings check (`INKT` against `Company.inks`) | whether it follows a cartridge swap |
| Font names on the test print | `hx_model.STANDARD_FONTS` | the exact names as read |
| Beacon | never used (`BEACON_SUPPORTED = False`) | architecture §11 |
| A Send made while the share is down | no expected state is written; the queued `send` log holds each machine's fingerprint | states.md: how the poller treats the running message when the share returns (it differs from `expected.json`) |

## 22. Decisions made in this spec

1. **Always alternate slots.** The idle slot is the other of 9 and 10 whenever 9 or 10 is selected,
   printing or not, so the selected slot is never rewritten during a Send. It implies the plan's rule.
2. **Message name** `{film}-{print text}{revision % 100:02d}-{content}-{check}{slot}`: hyphens for
   readability on the printer's screen, the last two digits of the revision (the full one is logged),
   a 4-character Crockford code from the first 20 bits of SHA-256, a check letter from SHA-256 over the
   rest of the name (24 letters, no I or O), and `9` or `0` for the slot. At most 20 characters with
   `FILM_CODE_MAX` 6, so it isn't lowered.
3. **The canonical design** is the JSON of the children sent, in order, without the name and slot, so
   the content code changes with anything printed but not with the date.
4. **Text before and after go inside TXT for text items**, and in PREF/POSF only for dates, so a text
   item reads back as exactly what prints.
5. **Relative placement**: "right" refers to the previous item; "below" lines up with the start of the
   previous line and goes under the previous item. One font and size per print text keeps lines even.
6. **Origin matching** uses four rules (words, punctuation removed, joined multi-word phrases, spelled-out
   initials), counts inactive countries, and checks printed codes. Aliases match ignoring case, as the
   plan says, so an alias such as `CAN` also refuses the word "can": administrators choose aliases
   knowing that. Matched words and country names may appear in messages (they are Stripes' own list
   entries); a sender's text never does.
7. **Bag text characters** are printable Latin-1 without `<` and `>` until the test unit shows how
   escaping prints.
8. **Eleven date formats** built from Y, y, M, m and D; Julian and week-day codes are left out of the
   first version because their printed form isn't known.
9. **Every read-back checks the rendered date** against Stripes' own rendering (±120 s), which also
   proves the clock, the rollover and the offset.
10. **The TD** is rounded to the nearest minute, always carries the rollover (0, 0) and HOUR 0 to 23,
    and is checked by reading DATE and TIME back (90 s tolerance, one re-read for midnight). A Send
    skips the "before" read; Set clocks and the daylight-saving set log it.
11. **One SNTP request per Send or Set clocks**, reused per machine with `now_from`; `host:port` is
    accepted so the fake time server can be used from `hx_fake.py`.
12. **The ink check also compares the cartridge type**, and an ink name not on the list fails.
13. **Encoder settings (ENWHD, ENPPR) are checked at every Send** on encoder machines, because a change
    moves every print.
14. **The pipeline re-merges the plan at its start** and refuses if the templates changed after the
    sender looked.
15. **A lock held on any printer of a line stops the whole line**, with a message that names the holder
    and what they are doing.
16. **Cancel** gives red results noted `cancelled`, writes nothing and opens no red window.
17. **The poller handshake gives up after `2 × stale_s`** (30 s), checking every 0.25 s.
18. **A failure before the switch keeps the old expectation** (message, fingerprint, confirmation) and
    adds the Send's id, the failure and red, so Leave it and the alerts can refer to it.
19. **"Something's wrong" is recorded as a failure** at `operator_confirm`, after the switch, as well as
    `rejected`.
20. **Green needs no amber note.** A confirmed Send made with the PC's time stays amber until a later
    Send, or Set clocks with the time server, clears the note.
21. **The confirmation is logged first**, then the expected states; the sender is identified from the
    Send's log entry, so reopening from another PC works.
22. **Stop, Resume and Set clocks are logged in hx-sends** (only senders do them); the daylight-saving
    set goes to hx-events through states.md.
23. **Resume also refuses** after a failed Send, an unconfirmed Send, a test print, or while a Send or
    test is in progress.
24. **A test step is confirmed with one running read-back** and a PRIN reading, not two: the step's
    label is proven to match what prints, and the steps keep up with about 25 bags a minute. PRINT 1 is
    sent at every step; the next step is built ahead; each step's count starts at its own switch.
25. **The test's ink mismatch is a warning**, not a stop: the machine runs empty film.
26. **Test steps**: the position or delay range first with the film's bold, width and resolution, then
    each bold, width and resolution value at the film's own position or delay; labels carry `P`/`D`
    always and `B`, `W`, `R` when those are varied. All test names use the code `TST`.
27. **Calibration plans** reuse the test print with the base machine's head distance set to 0, so the
    position is sent as it is; marks are aimed at the middle of the bag from the rough measurement.
28. **The print counts are for the log only**: a failed counter read leaves them empty and doesn't stop
    the Send.
29. **Internal errors** show only the exception's class name to the operator; the traceback goes to the
    event log.
30. **Lots and free text are stripped**; an optional empty free text leaves its whole item out; a lot is
    ignored when the print text has no lot item.
31. **Film codes are unique across all films**, and `TST` is reserved for test prints.
32. **The send log holds everything needed to rebuild the result window**: the exact commands, the
    expected items and settings, and all four pictures.
33. **Added fields**: `ExpectedState.stop_reason`, `stopped_by`, `stopped_at`, and `LineStatus.sender`.
34. **No item limit in practice**: `MAX_ITEMS` (99) only keeps OBJIDs inside the manual's range; the
    canvas is the real limit, and the designer previews the longest value (ui.md).
35. **Daylight-saving catch-up**: `recent_dst_change` lets a poller that started after the change still
    make the one automatic clock set, once, when the printer's clock is off by the change (§9.6 item 8,
    states.md decides whether to use it).
36. **No packaging change**: the output stays the single-file `Stripes.exe`, as the owner decided.
