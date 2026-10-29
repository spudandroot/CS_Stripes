# MapleJet Hx build spec: storage

Part 3 of 7. This file specifies `hx_share`: the v2 folders on the share and on each PC, every file
format, writing whole files and reading them safely on a Windows share, printer locks, heartbeats and
the poller assignment, the local copy and the 24-hour rule, the logs and the upload queue, the
permissions the folder script must set, fault injection and the tests. Names, types, layers and
threads come from architecture.md and are not changed here. New public names are only added inside
`hx_share`, and are all listed in §15.

Contents: 0 Conventions · 1 Folder layout · 2 File names by ID · 3 File envelope and format versions ·
4 Records · 5 Status files · 6 Logs · 7 PC files · 8 Whole-file writes and swap-safe reads ·
9 ShareStore: timeouts and share health · 10 Freshness · 11 Printer locks · 12 Heartbeats and poller
assignment · 13 DataService · 14 Permissions and the folder script · 15 Interfaces added here ·
16 Fault injection · 17 Tests · 18 Notes for the other spec files · 19 OPEN items ·
20 Decisions made in this spec

## 0. Conventions

1. "The share" means `HxPaths.share_root`: `\\SERVER\Share\Stripes\v2`, next to today's `config`
   folder (architecture.md §10.5). Paths in this file are relative to it and use `/`. Code joins them
   with `os.path.join(root, *rel.split("/"))`. "PC folders" means `HxPaths.roaming`, `.local` and
   `.machine`.
2. Every share access goes through `ShareStore`, and every operation runs on an `hx-io` thread with
   the 5 s timeout (§9). The Tk thread never calls `ShareStore`, `FileOps` or a blocking
   `DataService` method.
3. JSON is written as UTF-8 without a BOM. Readers accept a BOM (`utf-8-sig`, like
   `stripes._read_json`), so a file saved by Notepad or written by the PowerShell folder script still
   reads.
4. Times in files are epoch seconds from the writer's own `clock.time()`, and are used only for
   display and logs. Freshness and staleness are judged only from counters watched on the reader's
   own `clock.monotonic()` (§10).
5. Examples follow the public-repo rule: 192.0.2.x, UID 1109, `time.example.local`, "Example Co",
   "Line 1 Bagger 1", `EXAMPLE\jsmith`, computers `FLOOR01`/`FLOOR02`. The example IDs used throughout
   are line `ln_1a2b3c4d5e`, machine `mc_5d1e2f3a4b`, printer `pr_9c8b7a6d5e`, product `pd_7e6f5a4b3c`,
   film `fm_3f9a1c22b0`, print text `pt_0b1c2d3e4f` and Send `sd_8a7b6c5d4e`. The example time
   1793275200.0 is 29 Oct 2026 12:00 UTC (the `FakeClock` start).
6. Packaging doesn't change anything here. The output is still the single-file `Stripes.exe`, deployed
   as it is today, with no installer. Stripes creates every PC folder itself the first time it needs
   it, and nothing on the PC needs administrator rights. The share folders come from the folder
   script (§14).

## 1. Folder layout

### 1.1 On the share

```
\\SERVER\Share\Stripes\
  Stripes.exe                    unchanged: Group Policy copies it to each user, as today
  config\                        today's Zebra config (ConfigPath); read-only fallback for this version
  status\                        today's deployment log; read-only (Deployment status window)
  v2\                            HxPaths.share_root
    config\
      printers.json              Zebra printers, copied once by the folder script (today's format)
      stock.json                 Zebra label stock, copied once (today's format)
      hx\
        company.json             Company record
        changes.json             change counter, bumped by every admin save (§4.2)
        lines\<line_id>.json
        machines\<machine_id>.json
        printers\<printer_id>.json
        adjustments\<machine_id>__<film_id>.json
        calibrations\<machine_id>.json
    templates\
      products\<product_id>.json
      films\<film_id>\film.json
      films\<film_id>\files\<sha8>_<safe name>       originals: photos, artwork PDF, spec sheets
      films\<film_id>\previews\<sha256>.png          small photo previews made by the admin's PC
      print-texts\<print_text_id>.json
    status\
      deployment\<COMPUTER>_<user>.json              Zebra deployment log (stripes.DeploymentLog)
      machines\<machine_id>.poll.json                written only by the assigned poller
      machines\<machine_id>.preview.bmp              written only by the assigned poller
      machines\<machine_id>.expected.json            written by senders and testers under the lock
      lines\<line_id>.json                           written by senders
      locks\<printer_id>.lock                        printer lock
      alerts\<alert_id>.json                         one acknowledgement each, written once
      pollers\<heartbeat key>.json                   one heartbeat per running Stripes
    logs\
      hx-sends\<YYYY-MM>\<log name>                  add-only
      hx-events\<YYYY-MM>\<log name>                 add-only
      hx-admin\<YYYY-MM>\<log name>                  add-only (admin changes, test prints, calibration)
```

The folder script creates every folder above down to `config\hx\<type>\`, `templates\products\`,
`templates\films\`, `templates\print-texts\`, every `status\` subfolder and `logs\<kind>\`. Stripes
creates only `templates\films\<film_id>\` with its `files\` and `previews\` (administrators), and
the `logs\<kind>\<YYYY-MM>\` month folders. Every other missing folder is a set-up problem
(§9.2): Stripes reports it and never creates it.

### 1.2 On each PC

```
%APPDATA%\Stripes\v2\                 HxPaths.roaming: per user, roams with the profile
  settings.json                       Zebra settings, today's format (copied once from %APPDATA%\Stripes\settings.json)
  maplejet.json                       HxSettings (ui.md owns the fields), §7.1
%LOCALAPPDATA%\Stripes\v2\            HxPaths.local: per user, this PC only
  config-cache\printers.json          Zebra cache (stripes.cache_folder()), today's format
  config-cache\stock.json
  instance.txt                        single-instance note (stripes.SingleInstance)
  hx\
    local-copy.json                   the local copy, §7.2
    previews\<sha256>.png             photo previews for the local copy, §7.3
    reach.json                        when this user's Stripes last reached a printer, §12.3
    poller-<name>.json                poller memory; states.md owns the names and contents
    send-queue\                       fallback upload queue, used only if the PC queue can't be written, §7.5
%PROGRAMDATA%\Stripes\v2\             HxPaths.machine: shared by every user of this PC
  send-queue\<kind>~<log name>        a log entry waiting to upload, §7.5
  send-queue\<kind>~<log name>.uploaded   its upload marker
```

### 1.3 Old and new side by side

1. Today's version reads only `config\printers.json`, `config\stock.json`, the top level of `status\`,
   `%APPDATA%\Stripes\settings.json`, `%LOCALAPPDATA%\Stripes\config-cache\` and
   `%LOCALAPPDATA%\Stripes\instance.txt`. It never sees anything under any `v2`.
2. This version writes only under `v2` folders. It reads today's `ConfigPath` read-only, as a fallback
   until the folder script has run, and today's `status\` read-only for the Deployment status window
   (architecture.md §10.5).

## 2. File names by ID

| What | Path (on the share unless marked PC) | Written by | Read by |
|---|---|---|---|
| Company | `config/hx/company.json` | `DataService.save` (administrators); seeded by the folder script | everyone |
| Change counter | `config/hx/changes.json` | `DataService.save`; seeded by the folder script | everyone |
| Line | `config/hx/lines/<line_id>.json` | administrators | everyone |
| Machine | `config/hx/machines/<machine_id>.json` | administrators | everyone |
| Printer | `config/hx/printers/<printer_id>.json` | administrators | everyone |
| Machine + film adjustment | `config/hx/adjustments/<machine_id>__<film_id>.json` | administrators | everyone |
| Calibration | `config/hx/calibrations/<machine_id>.json` | administrators | everyone |
| Product | `templates/products/<product_id>.json` | administrators | everyone |
| Film | `templates/films/<film_id>/film.json` | administrators | everyone |
| Film original | `templates/films/<film_id>/files/<sha256[:8]>_<safe name>` | `DataService.save_file` | UI (opened in the default viewer) |
| Film photo preview | `templates/films/<film_id>/previews/<sha256>.png` | `DataService.save_preview` | everyone (copied to the local copy) |
| Print text | `templates/print-texts/<print_text_id>.json` | administrators | everyone |
| Poll status | `status/machines/<machine_id>.poll.json` | the assigned poller only | everyone |
| Running-message picture | `status/machines/<machine_id>.preview.bmp` | the assigned poller only | everyone |
| Expected state | `status/machines/<machine_id>.expected.json` | the printer-lock holder | everyone |
| Line status | `status/lines/<line_id>.json` | senders | everyone |
| Printer lock | `status/locks/<printer_id>.lock` | the lock holder | everyone |
| Lock set aside during a takeover | `status/locks/<printer_id>.lock.stale-<new token>` | the taker; removed at once | nobody |
| Acknowledgement | `status/alerts/<alert_id>.json` | whoever acknowledges, once | everyone |
| Heartbeat | `status/pollers/<heartbeat key>.json` | that Stripes process | pollers |
| Zebra deployment log | `status/deployment/<COMPUTER>_<user>.json` | `stripes.DeploymentLog` | Deployment window |
| Send log entry | `logs/hx-sends/<YYYY-MM>/<log name>` | senders | everyone |
| Event log entry | `logs/hx-events/<YYYY-MM>/<log name>` | everyone | everyone |
| Admin log entry | `logs/hx-admin/<YYYY-MM>/<log name>` | administrators | everyone |
| Temp file of a whole-file write | `<final name>.<tmp tag>-<6 hex>.tmp` (same folder) | the writer | nobody |
| Local copy (PC) | `<local>/hx/local-copy.json` | `LocalStore` | `LocalStore` |
| Photo preview cache (PC) | `<local>/hx/previews/<sha256>.png` | `LocalStore` | UI |
| Reach memory (PC) | `<local>/hx/reach.json` | `ReachMemory` | `ReachMemory` |
| Queued log entry (PC) | `<machine>/send-queue/<kind>~<log name>` | `LocalStore` | `DataService` |
| Upload marker (PC) | `<machine>/send-queue/<kind>~<log name>.uploaded` | `LocalStore` | `LocalStore` |

Name patterns. A write with a name that doesn't match raises `ValueError`, because that is a
programming error. Readers silently skip any name that doesn't match, including `desktop.ini`,
`Thumbs.db`, `~$*` and `*.tmp`.

| Name part | Pattern |
|---|---|
| Company id | `company` |
| Line, machine, printer, product, film, print-text id | `^(ln|mc|pr|pd|fm|pt)_[0-9a-f]{10}$` (`hx_model.new_id`) |
| Adjustment id | `^mc_[0-9a-f]{10}__fm_[0-9a-f]{10}$` (`hx_model.adjustment_id`) |
| Heartbeat key | `^[A-Za-z0-9._-]{1,120}$` (`Identity.key`) |
| Alert id | `^[A-Za-z0-9._-]{1,100}$` (states.md makes them; unique per occurrence) |
| sha256 | `^[0-9a-f]{64}$` |
| Month folder | `^\d{4}-(0[1-9]|1[0-2])$` (UTC month of the log name) |
| Log name | §6.3 |
| Attachment safe name | `re.sub(r"[^A-Za-z0-9._-]+", "_", stem)[:48] + re.sub(r"[^A-Za-z0-9.]+", "", ext)[:10]`, or `file` if that's empty |
| Temp tag | `sha256(f"{computer}|{user}")[:12]` + `-` + pid, e.g. `c6a3264501b9-4242` (set by `ShareStore`, §8) |

Path length: every share-relative path stays at or under 140 characters, so with a share root of up to
100 characters a full path stays under Windows' 260-character `MAX_PATH`. Stripes doesn't rely on
long-path support. The longest paths are a heartbeat (`status/pollers/` + up to 120 + `.json`) and a
log entry (about 106).

## 3. File envelope and format versions

### 3.1 The envelope

Every JSON file that hx code writes is one JSON object. It starts with two envelope keys, followed by
the fields of its dataclass as `hx_model.to_json` produces them (snake_case field names, in field
order).

| Key | Type | Value |
|---|---|---|
| `format_version` | int | `hx_model.FORMAT` (1) when written |
| `file_type` | str | one of: `company`, `changes`, `line`, `machine`, `printer`, `adjustment`, `calibration`, `product`, `film`, `print_text`, `poll`, `expected`, `line_status`, `lock`, `heartbeat`, `ack`, `log`, `local_copy`, `user_settings`, `local_state`, `upload_marker` |

`encode_json(file_type, data) -> bytes` returns `{"format_version": FORMAT, "file_type": file_type,
**data}` as `json.dumps(obj, ensure_ascii=False, indent=1, allow_nan=False) + "\n"` in UTF-8. Values
JSON can't hold raise `TypeError` (for example `bytes`: callers store binary as base64 text) or
`ValueError` (NaN, infinity). `from_json` ignores unknown keys, so the envelope keys never reach a
dataclass.

### 3.2 Reading

`decode_json(raw, *, file_type, rel) -> dict`:

1. Decode with `utf-8-sig` and run `json.loads`. If it isn't a dict, raise `ValueError` ("damaged").
2. `format_version` must be an int (not a bool) of at least 1. Otherwise raise `ValueError`.
3. If `format_version > FORMAT`, raise `FormatTooNew(Msg(N_("Update Stripes: {file} was saved by a
   newer version."), {"file": rel}), path=rel, found=format_version)`.
4. If `file_type` is given and `d["file_type"]` differs, raise `ValueError`.
5. If `format_version < FORMAT`, apply `UPGRADES[v]` for each version from v up to FORMAT − 1, in
   order. The dict is empty while FORMAT is 1.
6. Return the dict, envelope keys included.

Typed readers then call `from_json(cls, d)`. A `ValueError` there also means "damaged". What
happens with a damaged file depends on what it is: see §5 for status files, §6.6 for logs and §13.2
for records.

### 3.3 The format-version rule

1. Writers always write `FORMAT`.
2. A reader accepts any version up to `FORMAT` and upgrades older ones in memory (§3.2 step 5). It
   never rewrites a file just to upgrade it.
3. `FORMAT` goes up only for a change that an older version would misread: a field removed, renamed,
   or given a new meaning or unit. A new optional field with a safe default doesn't bump it. Older
   versions ignore the field, and `DataService.save` keeps keys it doesn't know (§13.4).
4. A newer record file (in `config/hx` or `templates`) makes this PC refuse to send at all.
   `DataService.can_send()` returns False with "Update Stripes", and admin saves of that record raise
   `FormatTooNew`. Polling carries on with the last good data.
5. A newer status file is not readable here. Readers still use its stable keys (§3.4) wherever
   freshness or locking depends on them, so an older PC never mistakes a newer PC's live poll status
   or lock for an absent one.
6. Heartbeats count only when `format_version == FORMAT` exactly (§12.2). PCs on different formats
   therefore never share an assignment. The poll-file invariant (§12.6) still stops them from both
   polling one printer.
7. A newer log entry is skipped by readers (§6.6). A newer local file (local copy, settings, reach
   memory) is treated as missing.

### 3.4 Stable keys

These keys keep their name and meaning in every future format, so older readers can still use them
from a newer file.

| File | Stable keys |
|---|---|
| every JSON file | `format_version`, `file_type` |
| `poll` | `counter`, `machine_id`, `printer_id`, `poller`, `paused_for_lock`, `yielded_to` |
| `lock` | `counter`, `printer_id`, `token`, `user`, `computer`, `purpose` |
| `expected`, `line_status`, `heartbeat` | `counter` |
| `ack` | `alert_id` |
| `log` (line 2 of the file) | `end` |

### 3.5 Files without the envelope

| File | Why |
|---|---|
| `config/printers.json`, `config/stock.json`, `config-cache/*` | Zebra files in today's format, edited by IT and read by the unchanged Zebra code |
| `settings.json` (Zebra per-user settings), `instance.txt` | today's formats |
| `status/deployment/*.json` | written by today's `DeploymentLog` code |
| `*.bmp`, `*.png`, film originals | binary. Each is referenced by sha256 from a JSON file that has an envelope |

## 4. Records

Rules for every record file:

1. One file per record, named after its id. The exception is films: each film has a folder, and the
   record is `film.json` inside it.
2. The content is `encode_json(<file_type>, to_json(record))`. The `id` inside must equal the id in
   the path; if it doesn't, the file counts as damaged.
3. Only `DataService.save` writes record files (§13.4). `write_record` (§15) exists for seeding test
   worlds. The folder script writes only the first `company.json` and `changes.json`.
4. Records are never deleted. Not active means `active: false`, or `status: "inactive"` for films and
   print texts.
5. Base fields of every record (`hx_model.Record`): `id` (str), `revision` (int: 1 after the first
   save, then +1 on every save), `updated_by` (str, `Identity.user`), `updated_at` (float or null).
6. Size limit: 1,000,000 bytes per record file (`RECORD_MAX_BYTES`). A bigger one is refused on save
   with `ValueError` and counts as damaged on load.

### 4.1 Company: `config/hx/company.json`

`file_type` `company`. Class `Company`, id always `"company"`. Fields: `time_server` str ·
`time_server_fallback` str · `countries` list of `{name: str, aliases: list[str], active: bool}` ·
`inks` list of `{name: str, cartridge: str, colour: str}` · `lot_min_digits` int · `lot_max_digits`
int · `free_text_max` int · `admin_group` str · `admin_group_sid` str · `sender_group` str ·
`sender_group_sid` str · `standard_fonts` list[str] · `bag_counter` str.

`bag_counter` is `"PMCNT"`, `"MSCNT"` or `"none"`, with default `"PMCNT"`. OPEN: confirm on the test
unit which counter counts bags. `standard_fonts` keeps the default spelling, also OPEN (architecture.md
§5.1).

```json
{
 "format_version": 1,
 "file_type": "company",
 "id": "company",
 "revision": 3,
 "updated_by": "EXAMPLE\\jsmith",
 "updated_at": 1793271600.0,
 "time_server": "time.example.local",
 "time_server_fallback": "192.0.2.123",
 "countries": [
  {"name": "USA", "aliases": ["US", "U.S.A.", "United States"], "active": true},
  {"name": "Canada", "aliases": ["CAN", "CA"], "active": true},
  {"name": "Mexico", "aliases": ["MEX", "MX", "México"], "active": true}
 ],
 "inks": [
  {"name": "Example Black Plastic", "cartridge": "plastic", "colour": "black"},
  {"name": "Example White Paper", "cartridge": "paper", "colour": "white"}
 ],
 "lot_min_digits": 4,
 "lot_max_digits": 8,
 "free_text_max": 30,
 "admin_group": "Stripes Administrators",
 "admin_group_sid": "S-1-5-21-1000000001-1000000002-1000000003-2101",
 "sender_group": "Stripes MapleJet Senders",
 "sender_group_sid": "S-1-5-21-1000000001-1000000002-1000000003-2102",
 "standard_fonts": ["CourierN", "Arial", "ArialNarrow", "ArialNarrowB", "TimesNewRom", "LinearBeam"],
 "bag_counter": "PMCNT"
}
```

### 4.2 Change counter: `config/hx/changes.json`

`file_type` `changes`. It isn't a record. It lets every PC skip the full reload while nothing has
changed (§13.2). Fields: `counter` int (+1 on every save) · `written_at` float · `written_by` str ·
`last` {`file_type`: str, `id`: str, `revision`: int} or null. Two administrators saving at the same
moment can lose one increment. That's harmless, because any change of the value triggers a reload.

```json
{
 "format_version": 1,
 "file_type": "changes",
 "counter": 57,
 "written_at": 1793271600.0,
 "written_by": "EXAMPLE\\jsmith",
 "last": {"file_type": "film", "id": "fm_3f9a1c22b0", "revision": 4}
}
```

### 4.3 Line: `config/hx/lines/<line_id>.json`

`file_type` `line`. Class `Line`. Fields: `name` str · `code` str (1–3 characters) · `cartridge` str
(`paper`|`plastic`) · `notify_user` str · `active` bool.

```json
{
 "format_version": 1,
 "file_type": "line",
 "id": "ln_1a2b3c4d5e",
 "revision": 2,
 "updated_by": "EXAMPLE\\jsmith",
 "updated_at": 1793268000.0,
 "name": "Line 1",
 "code": "L1",
 "cartridge": "plastic",
 "notify_user": "EXAMPLE\\jsmith",
 "active": true
}
```

### 4.4 Machine: `config/hx/machines/<machine_id>.json`

`file_type` `machine`. Class `Machine`. Fields: `name` str · `code` str · `line_id` str ·
`printer_id` str · `placement` str (`encoder`|`delay`) · `head_distance_mm` float|null ·
`encoder_wheel_mm` float|null · `encoder_ppr` int|null · `print_speed` int|null · `rotate` bool ·
`mirror` bool · `seconds_per_bag` float · `active` bool.

Three of these hold OPEN values with safe defaults (architecture.md §11):

- `print_speed`: units and range are OPEN. It is null until an administrator enters it, and a null
  value blocks sending.
- `head_distance_mm`: comes from encoder calibration (OPEN). Null blocks sending on encoder machines.
- `seconds_per_bag`: used for timed test steps (OPEN).

```json
{
 "format_version": 1,
 "file_type": "machine",
 "id": "mc_5d1e2f3a4b",
 "revision": 5,
 "updated_by": "EXAMPLE\\jsmith",
 "updated_at": 1793268000.0,
 "name": "Line 1 Bagger 1",
 "code": "B1",
 "line_id": "ln_1a2b3c4d5e",
 "printer_id": "pr_9c8b7a6d5e",
 "placement": "delay",
 "head_distance_mm": null,
 "encoder_wheel_mm": null,
 "encoder_ppr": null,
 "print_speed": 2400,
 "rotate": false,
 "mirror": false,
 "seconds_per_bag": 2.4,
 "active": true
}
```

### 4.5 Printer: `config/hx/printers/<printer_id>.json`

`file_type` `printer`. Class `Printer`. Fields: `name` str · `host` str · `port` int · `uid` int ·
`token` str · `active` bool.

`token` is the scrambled form, `hx_protocol.scramble(make_token(password), release_key())`, made by
the admin screen before saving. It is `""` until a password is set. Storage never sees a password and
never writes the token into a log: admin log entries replace a non-empty token with `"(set)"`
(§13.4). Every PC can read it, by design (plan: "obscurity, chosen on purpose"), and the local copy
holds it so standalone polling works.

```json
{
 "format_version": 1,
 "file_type": "printer",
 "id": "pr_9c8b7a6d5e",
 "revision": 1,
 "updated_by": "EXAMPLE\\jsmith",
 "updated_at": 1793264400.0,
 "name": "Printer 1",
 "host": "192.0.2.10",
 "port": 80,
 "uid": 1109,
 "token": "SCRAMBLED-TOKEN-EXAMPLE",
 "active": true
}
```

### 4.6 Machine + film adjustment: `config/hx/adjustments/<machine_id>__<film_id>.json`

`file_type` `adjustment`. Class `MachineFilmAdjustment`. The id is `adjustment_id(machine_id,
film_id)`, and `machine_id` and `film_id` inside must match it. Fields: `machine_id` str · `film_id`
str · `position_mm` float · `delay_ms` float. A file exists only once an adjustment has been set; a
missing file means 0 and 0. `updated_by`/`updated_at` record who set it and when.

```json
{
 "format_version": 1,
 "file_type": "adjustment",
 "id": "mc_5d1e2f3a4b__fm_3f9a1c22b0",
 "revision": 2,
 "updated_by": "EXAMPLE\\jsmith",
 "updated_at": 1793271600.0,
 "machine_id": "mc_5d1e2f3a4b",
 "film_id": "fm_3f9a1c22b0",
 "position_mm": 0.0,
 "delay_ms": 20.0
}
```

### 4.7 Calibration: `config/hx/calibrations/<machine_id>.json`

`file_type` `calibration`. Class `Calibration`, with id = `machine_id`. Fields: `machine_id` str ·
`spacing_sent_mm` int · `spacing_measured_mm` float|null · `spacing_by` str · `spacing_at`
float|null · `rough_head_distance_mm` float|null · `head_print_sent_mm` int|null ·
`head_print_measured_mm` float|null · `head_distance_mm` float|null · `head_by` str · `head_at`
float|null.

How the head distance is worked out is OPEN (architecture.md §11). Saving a calibration whose
`head_distance_mm` differs from the machine's also saves the machine with the value copied (§13.4).

```json
{
 "format_version": 1,
 "file_type": "calibration",
 "id": "mc_5d1e2f3a4b",
 "revision": 1,
 "updated_by": "EXAMPLE\\jsmith",
 "updated_at": 1793271600.0,
 "machine_id": "mc_5d1e2f3a4b",
 "spacing_sent_mm": 300,
 "spacing_measured_mm": 300.5,
 "spacing_by": "EXAMPLE\\jsmith",
 "spacing_at": 1793268000.0,
 "rough_head_distance_mm": 400.0,
 "head_print_sent_mm": 348,
 "head_print_measured_mm": 136.0,
 "head_distance_mm": 412.0,
 "head_by": "EXAMPLE\\jsmith",
 "head_at": 1793271600.0
}
```

### 4.8 Product: `templates/products/<product_id>.json`

`file_type` `product`. Class `Product`. Fields: `name` str · `customer` str · `active` bool.

```json
{
 "format_version": 1,
 "file_type": "product",
 "id": "pd_7e6f5a4b3c",
 "revision": 1,
 "updated_by": "EXAMPLE\\jsmith",
 "updated_at": 1793264400.0,
 "name": "Example Co 2lb Onion",
 "customer": "Example Co",
 "active": true
}
```

### 4.9 Film: a folder per film, `templates/films/<film_id>/`

| Path | What |
|---|---|
| `film.json` | the `Film` record, `file_type` `film` |
| `files/<sha256[:8]>_<safe name>` | each original added with `save_file`, never overwritten (a different file gets a different sha prefix) |
| `previews/<sha256>.png` | a small PNG preview of an image original, keyed by the original's sha256 (§13.5) |

Fields of `film.json`: `product_id` str · `code` str · `artwork_revision` str · `status` str
(`development`|`active`|`inactive`) · `files` list of FileRef · `side` str · `cartridge` str ·
`ink_colour` str · `length_mm` float|null · `position_mm` float|null · `delay_ms` float|null ·
`print_length_mm` float|null (OPEN: the film-stop warning) · `font` int · `size` int · `resolution`
int · `bold` int · `width` int · `origin_preprinted` bool · `preprinted_origin` str ·
`print_text_ids` list[str] · `default_print_text_id` str.

The fields of a FileRef are `kind` str (`front_photo`|`back_photo`|`artwork`|`spec`|`other`), `name`
str, `path` str, `sha256` str, `size` int, `added_by` str and `added_at` float|null. `path` is
relative to `v2/templates` with forward slashes, and must start with `films/<this film id>/files/`.
Anything else counts as damaged. Originals that no film references are orphans: harmless, and never
deleted by Stripes.

```json
{
 "format_version": 1,
 "file_type": "film",
 "id": "fm_3f9a1c22b0",
 "revision": 4,
 "updated_by": "EXAMPLE\\jsmith",
 "updated_at": 1793271600.0,
 "product_id": "pd_7e6f5a4b3c",
 "code": "EXON2",
 "artwork_revision": "Rev C",
 "status": "active",
 "files": [
  {"kind": "front_photo", "name": "front.jpg",
   "path": "films/fm_3f9a1c22b0/files/0e475da7_front.jpg",
   "sha256": "0e475da77f3e10d2f36222f0395039ac6ed5abe8b52a0eddbf05bf75e0c42bd8",
   "size": 2481337, "added_by": "EXAMPLE\\jsmith", "added_at": 1793268000.0},
  {"kind": "back_photo", "name": "back.jpg",
   "path": "films/fm_3f9a1c22b0/files/f910d595_back.jpg",
   "sha256": "f910d59536174593e483d96adceaa6d0c18a3ea558ea70916be7b6b6ba0d204d",
   "size": 2390112, "added_by": "EXAMPLE\\jsmith", "added_at": 1793268000.0}
 ],
 "side": "back",
 "cartridge": "plastic",
 "ink_colour": "black",
 "length_mm": 610.0,
 "position_mm": 150.0,
 "delay_ms": 480.0,
 "print_length_mm": null,
 "font": 2,
 "size": 4,
 "resolution": 0,
 "bold": 1,
 "width": 1,
 "origin_preprinted": false,
 "preprinted_origin": "",
 "print_text_ids": ["pt_0b1c2d3e4f"],
 "default_print_text_id": "pt_0b1c2d3e4f"
}
```

### 4.10 Print text: `templates/print-texts/<print_text_id>.json`

`file_type` `print_text`. Class `PrintText`. Fields: `name` str · `code` str · `status` str ·
`items` list of PrintTextItem. The fields of a PrintTextItem are `id` str, `kind` str, `before` str,
`after` str, `text` str, `date_format` str, `offset` int, `offset_unit` str, `label` str, `max_len`
int, `required` bool, `placement` str and `gap` int (OPEN: units). `before`, `after` and `text` are bag
text: stored exactly as typed and never translated.

```json
{
 "format_version": 1,
 "file_type": "print_text",
 "id": "pt_0b1c2d3e4f",
 "revision": 7,
 "updated_by": "EXAMPLE\\jsmith",
 "updated_at": 1793264400.0,
 "name": "Packed on + origin",
 "code": "PKO",
 "status": "active",
 "items": [
  {"id": "i1", "kind": "packed_date", "before": "Packed on ", "after": "", "text": "", "date_format": "MM/DD/YY",
   "offset": 0, "offset_unit": "days", "label": "", "max_len": 0, "required": true, "placement": "right", "gap": 0},
  {"id": "i2", "kind": "origin", "before": "", "after": "", "text": "", "date_format": "",
   "offset": 0, "offset_unit": "days", "label": "", "max_len": 0, "required": true, "placement": "below", "gap": 0},
  {"id": "i3", "kind": "lot", "before": "Lot ", "after": "", "text": "", "date_format": "",
   "offset": 0, "offset_unit": "days", "label": "", "max_len": 0, "required": true, "placement": "right", "gap": 4}
 ]
}
```

## 5. Status files

### 5.1 Overview

| File | Only writer | When written | Read how |
|---|---|---|---|
| `status/machines/<id>.poll.json` | the assigned poller | every cycle (about every `poll_fast_s`), and straight away when it pauses for a lock or yields | by name, every `watch_s` (watcher) and while a sender waits (§11.6) |
| `status/machines/<id>.preview.bmp` | the assigned poller | when the running message's picture changes, before the `poll.json` that names it | by name when `running.preview_sha256` changes |
| `status/machines/<id>.expected.json` | the holder of that printer's lock (Send, test, Stop, Resume, Set clocks, confirm, Leave it) | at each change of the expected state | by name, by the poller every cycle and by watchers |
| `status/lines/<id>.json` | senders | after a Send, a confirmation or a stop | by name, by watchers |
| `status/locks/<printer_id>.lock` | the lock holder | acquire, every `lock_refresh_s`, removed at release | by name, by the poller before every request and by watchers |
| `status/alerts/<alert_id>.json` | whoever acknowledges | once | by name, only for alerts that are active |
| `status/pollers/<key>.json` | that process | every `heartbeat_s`; removed on a clean exit | listing plus open, by pollers |

General rules:

1. Status files are written whole and swapped in (`write_bytes`, §8), except acks and new locks,
   which are created exclusively and written once.
2. Status files are never queued. While the share is down they simply aren't written. Every reader
   copes with a missing file.
3. A damaged status file is treated as missing, for staleness too (§10), except a damaged lock (§11.7).
4. A status file from a newer format raises `FormatTooNew` from its typed reader. Before raising,
   the store still feeds the file's stable `counter` to the watch (§3.4), so its freshness is known.
5. Size limit: 1,000,000 bytes per status JSON (`STATUS_MAX_BYTES`), and the same for the BMP.

### 5.2 Poll status: `status/machines/<machine_id>.poll.json`

`file_type` `poll`. Class `PollStatus`. architecture.md §5.8 owns the fields and states.md fills them.
Fields: `machine_id` str · `printer_id` str · `poller` str · `counter` int · `written_at` float ·
`paused_for_lock` str · `yielded_to` str · `reachable` bool · `failures` int · `last_answer_at`
float|null · `printing` bool|null · `message_name` str|null · `bag_count` int|null · `bags_recent`
int|null · `ink_name` str|null · `ink_level` int|null · `head_temp_c` int|null · `clock_offset_s`
float|null · `running` RunningMessage|null (`slot` int, `name` str, `fingerprint` str, `origin_line`
str, `read_at` float, `preview_sha256` str) · `daily` dict · `state` MachineSnapshot|null (`code` str,
`colour` str, `headline` Msg, `since` float, `detail` list[Msg]) · `alerts` list of Alert (`id` str,
`machine_id` str, `line_id` str, `kind` str, `sound` bool, `msg` Msg, `started_at` float, `recipients`
list[str], `active` bool, `colour` str, `ref` str, `acked_by` str, `acked_at` float|null, `ended_at`
float|null, `waiting` list[str]) · `foreign_client` bool · `problem` str · `expected_counter` int ·
`reread_seen` str · `unreachable_since` float|null · `running_ok_at` float|null · `running_unchecked`
bool · `dst` dict · `standalone` bool. The fields after `foreign_client`, and the Alert fields after
`active`, are states.md's additions (architecture.md §5.8 lists them all).

Write rules, enforced by `ShareStore.write_poll`:

1. It sets `poller = identity.key` and `written_at = clock.time()` on the copy it writes.
2. `counter` is the caller's. It must be greater than every counter this store has written or read
   for that machine; otherwise `ValueError`. A poller taking over continues from the file's counter
   + 1, never from 1.
3. The poller writes the picture (§5.3) before the `poll.json` whose `running.preview_sha256` names
   it.

```json
{
 "format_version": 1,
 "file_type": "poll",
 "machine_id": "mc_5d1e2f3a4b",
 "printer_id": "pr_9c8b7a6d5e",
 "poller": "FLOOR01_EXAMPLE_jsmith_4242",
 "counter": 18734,
 "written_at": 1793275205.12,
 "paused_for_lock": "",
 "yielded_to": "",
 "reachable": true,
 "failures": 0,
 "last_answer_at": 1793275205.05,
 "printing": true,
 "message_name": "(message name, send.md format)",
 "bag_count": 436512,
 "bags_recent": 118,
 "ink_name": "Example Black Plastic",
 "ink_level": 3,
 "head_temp_c": 26,
 "clock_offset_s": 1.4,
 "running": {
  "slot": 9,
  "name": "(message name, send.md format)",
  "fingerprint": "fd2ec5b521ad183152a5a69ee0acda6c271abbc60a97da48942225da41214566",
  "origin_line": "Product of USA and Canada",
  "read_at": 1793275170.4,
  "preview_sha256": "85f07f152f7ad57f448fc756933d8070b02293ba0979f09d8aa23a7916f94ca8"
 },
 "daily": {},
 "state": {"code": "(states.md code)", "colour": "green", "headline": {"text": "Printing", "values": {}},
           "since": 1793270000.0, "detail": []},
 "alerts": [],
 "foreign_client": false,
 "problem": "",
 "expected_counter": 42,
 "reread_seen": "",
 "unreachable_since": null,
 "running_ok_at": 1793275170.4,
 "running_unchecked": false,
 "dst": {},
 "standalone": false
}
```

### 5.3 Running-message picture: `status/machines/<machine_id>.preview.bmp`

These are the bytes of the 1-bit 1024 × 160 BMP from the running message's web Load: about 20.5 KB,
1,000,000 bytes at most. `read_preview(machine_id)` returns the bytes or None. A reader compares
`hashlib.sha256(bytes)` with `running.preview_sha256` from the `poll.json` it just read. If they
differ, the picture was swapped between the two reads; the reader keeps showing what it had and tries
again on its next read.

### 5.4 Expected state: `status/machines/<machine_id>.expected.json`

`file_type` `expected`. Class `ExpectedState`. send.md owns what goes in it. Fields: `machine_id` str ·
`counter` int · `written_at` float · `written_by` str · `computer` str · `mode` str · `printing_expected`
bool · `send_id` str · `message_name` str · `content_code` str · `fingerprint` str · `slot` int|null ·
`origin_line` str · `previous_message_name` str · `previous_fingerprint` str · `result` str ·
`failure` FailureInfo|null (`step` str, `msg` Msg, `after_switch` bool, `at` float) · `notes`
list[str] · `confirmed` bool · `confirmed_by` str · `confirmed_at` float|null · `rejected` bool ·
`resume_blocked` bool · `left_by` str · `left_at` float|null · `reread_token` str · `test` dict|null ·
`sender` str · `stop_reason` str · `stopped_by` str · `stopped_at` float|null. The last four are
send.md's additions (architecture.md §5.8 lists them all).

Write rules (`ShareStore.write_expected`):

1. The caller holds the printer's lock (architecture.md decision 24). The store doesn't check this.
2. The store reads the current file and writes a copy with `counter` = the file's counter + 1 (1 if
   the file is missing or damaged), `written_at = clock.time()`, `written_by = identity.user` and
   `computer = identity.computer`.
3. If the current file is from a newer format, `write_expected` raises `FormatTooNew` and doesn't
   write.
4. The store remembers, in memory, the last `ExpectedState` it read or wrote for each machine (a
   missing file is remembered as None). `cached_expected(machine_id) -> ExpectedState | None` returns
   that copy without touching the share; send.md §10.3 uses it when the share is down.

```json
{
 "format_version": 1,
 "file_type": "expected",
 "machine_id": "mc_5d1e2f3a4b",
 "counter": 42,
 "written_at": 1793275230.8,
 "written_by": "EXAMPLE\\jsmith",
 "computer": "FLOOR01",
 "mode": "running",
 "printing_expected": true,
 "send_id": "sd_8a7b6c5d4e",
 "message_name": "(message name, send.md format)",
 "content_code": "(send.md)",
 "fingerprint": "fd2ec5b521ad183152a5a69ee0acda6c271abbc60a97da48942225da41214566",
 "slot": 9,
 "origin_line": "Product of USA and Canada",
 "previous_message_name": "",
 "previous_fingerprint": "",
 "result": "amber",
 "failure": null,
 "notes": [],
 "confirmed": false,
 "confirmed_by": "",
 "confirmed_at": null,
 "rejected": false,
 "resume_blocked": false,
 "left_by": "",
 "left_at": null,
 "reread_token": "",
 "test": null,
 "sender": "EXAMPLE\\jsmith",
 "stop_reason": "",
 "stopped_by": "",
 "stopped_at": null
}
```

### 5.5 Line status: `status/lines/<line_id>.json`

`file_type` `line_status`. Class `LineStatus`. send.md owns what goes in it, and it is informational
only: colours come from the machine files. Fields: `line_id` str · `counter` int · `written_at` float ·
`written_by` str · `send_id` str · `sent_at` float|null · `product_id` str · `film_id` str ·
`film_revision` int · `print_text_id` str · `print_text_revision` int · `lot` str · `countries`
list[str] · `origin_line` str · `machines` dict[str, str] · `confirmed` bool · `confirmed_by` str ·
`stopped_needs_send` bool · `sender` str (send.md's addition). `write_line_status` sets `counter` (file
+ 1), `written_at` and `written_by`, the same way as `write_expected`.

```json
{
 "format_version": 1,
 "file_type": "line_status",
 "line_id": "ln_1a2b3c4d5e",
 "counter": 9,
 "written_at": 1793275231.0,
 "written_by": "EXAMPLE\\jsmith",
 "send_id": "sd_8a7b6c5d4e",
 "sent_at": 1793275222.5,
 "product_id": "pd_7e6f5a4b3c",
 "film_id": "fm_3f9a1c22b0",
 "film_revision": 4,
 "print_text_id": "pt_0b1c2d3e4f",
 "print_text_revision": 7,
 "lot": "4417",
 "countries": ["USA", "Canada"],
 "origin_line": "Product of USA and Canada",
 "machines": {"mc_5d1e2f3a4b": "amber"},
 "confirmed": false,
 "confirmed_by": "",
 "stopped_needs_send": false,
 "sender": "EXAMPLE\\jsmith"
}
```

### 5.6 Alerts and acknowledgements

1. Alerts are part of the poller's `poll.json` (`alerts`), so they have exactly one writer. Pollers
   never write in `status/alerts`; they only read it.
2. Each acknowledgement is its own file, `status/alerts/<alert_id>.json`, with `file_type` `ack` and
   class `Ack`. Fields: `alert_id` str · `by` str · `computer` str · `at` float.
3. `write_ack(ack)` uses `create_exclusive`. If the file already exists, the call does nothing:
   the first acknowledgement wins, and a second one isn't an error. Ack files are never changed or
   deleted by Stripes.
4. `read_ack(alert_id)` opens the file by name and never lists the folder. A found ack is cached in
   memory for the life of the store, because it never changes. A missing one isn't cached.
5. The alert id must be unique for each occurrence of an alert (states.md), so an old ack never
   silences a new alert of the same kind.

```json
{
 "format_version": 1,
 "file_type": "ack",
 "alert_id": "(states.md alert id)",
 "by": "EXAMPLE\\akim",
 "computer": "FLOOR02",
 "at": 1793275300.2
}
```

### 5.7 Heartbeat: `status/pollers/<key>.json`

`file_type` `heartbeat`. Class `HeartbeatInfo`. Fields: `key` str · `computer` str · `user` str ·
`pid` int · `version` str · `counter` int · `written_at` float · `can_poll` bool ·
`last_printer_reach` float|null · `polling` list[str] · `monitor_open` bool · `floor_board` bool.
The protocol is in §12.

```json
{
 "format_version": 1,
 "file_type": "heartbeat",
 "key": "FLOOR01_EXAMPLE_jsmith_4242",
 "computer": "FLOOR01",
 "user": "EXAMPLE\\jsmith",
 "pid": 4242,
 "version": "v2.0.0",
 "counter": 3311,
 "written_at": 1793275205.0,
 "can_poll": true,
 "last_printer_reach": 1793275204.9,
 "polling": ["pr_9c8b7a6d5e"],
 "monitor_open": true,
 "floor_board": false
}
```

### 5.8 Printer lock: `status/locks/<printer_id>.lock`

`file_type` `lock`. Class `LockInfo`. Fields: `printer_id` str · `token` str (16 hex,
`secrets.token_hex(8)`, new for every acquisition) · `user` str · `computer` str · `pid` int ·
`purpose` str (`LOCK_PURPOSES`) · `line_id` str · `machine_id` str · `run_id` str · `counter` int
(1 at creation, +1 at every refresh) · `acquired_at` float · `note` dict. The protocol is in §11.

```json
{
 "format_version": 1,
 "file_type": "lock",
 "printer_id": "pr_9c8b7a6d5e",
 "token": "a3f9c2e17b4d8e01",
 "user": "EXAMPLE\\jsmith",
 "computer": "FLOOR01",
 "pid": 4242,
 "purpose": "send",
 "line_id": "ln_1a2b3c4d5e",
 "machine_id": "mc_5d1e2f3a4b",
 "run_id": "sd_8a7b6c5d4e",
 "counter": 4,
 "acquired_at": 1793275215.0,
 "note": {}
}
```

## 6. Logs

### 6.1 Folders

`logs/hx-sends`, `logs/hx-events` and `logs/hx-admin` are add-only (§14). Writers can create files
and month folders, but nobody can change, rename or delete what is there. Logs are kept indefinitely,
and Stripes never deletes or rewrites a log file. Each entry is one file in the month folder of its
UTC timestamp: `logs/<kind>/<YYYY-MM>/<log name>`.

### 6.2 Entry rules

`append_log(kind, entry)` takes a plain dict (the "entry") from the owning spec. Storage requires:

1. `kind` is in `LOG_KINDS`.
2. `entry["type"]` is a string matching `^[a-z][a-z0-9_]{0,23}$`. Each spec owns its own types (§6.7).
3. The entry contains none of the reserved keys `format_version`, `file_type`, `log_kind`,
   `logged_at`, `logged_by`, `logged_on`, `logged_pid` or `log_name`.
4. Everything in it is JSON: str, int, float (finite), bool, None, list, dict. `Msg`s are stored as
   `Msg.to_json()`. Binary data, such as read-back pictures, is stored as base64 text under keys
   ending in `_b64`.
5. The encoded file is at most 8,000,000 bytes (`LOG_ENTRY_MAX_BYTES`).

A violation raises `ValueError` (a programming error). The stored object is the entry plus the
envelope, built by `make_log_record`:

| Key | Type | Value |
|---|---|---|
| `format_version` | int | `FORMAT` |
| `file_type` | str | `"log"` |
| `log_kind` | str | `hx-sends`, `hx-events` or `hx-admin` |
| `logged_at` | float | the writer's `clock.time()` |
| `logged_by` | str | `Identity.user` |
| `logged_on` | str | `Identity.computer` |
| `logged_pid` | int | `Identity.pid` |
| ...the entry's own keys | | as given, `type` included |

### 6.3 File names

`log_name(entry, at) -> str` returns `<stamp>.<type>.<ref>.<rand>.json`:

| Part | Rule | Example |
|---|---|---|
| stamp | `ms = int(at * 1000)`; `time.strftime("%Y%m%dT%H%M%S", time.gmtime(ms // 1000)) + f"{ms % 1000:03d}Z"` | `20261029T120005123Z` |
| type | `entry["type"]` | `send` |
| ref | the first non-empty of `send_id`, `run_id`, `action_id`, `record_id`, `film_id`, `machine_id`, `printer_id`, `line_id` in the entry, with `[^A-Za-z0-9_-]` replaced by `_` and cut to 24 characters; `x` if none | `sd_8a7b6c5d4e` |
| rand | `secrets.token_hex(3)` | `4be1a9` |

The full pattern is
`^(\d{8}T\d{9}Z)\.([a-z][a-z0-9_]{0,23})\.([A-Za-z0-9_-]{1,24})\.([0-9a-f]{6})(?:-([2-9]))?\.json$`.
The optional `-2` to `-9` suffix appears only when an upload finds an unfinished file with its name
(§6.5). `log_month(name)` is `name[0:4] + "-" + name[4:6]`. Names sort by time. The dots make the
parts unambiguous, so filters match exact parts and never substrings.

### 6.4 File content and the completion marker

A log file has exactly two lines, each ending in `\n`:

1. Line 1: the stored object as `json.dumps(stored, ensure_ascii=False, separators=(",", ":"),
   allow_nan=False)` in UTF-8. JSON escapes newlines inside strings, so this is one line.
2. Line 2: the completion marker `{"end":"<sha256 hex of the line-1 bytes, without its \n>"}`.

`encode_log(stored) -> bytes` builds this. `decode_log(raw) -> dict | None` returns the stored object
only when all of these hold: the bytes end with `\n`; they split into exactly two lines; line 2 parses
to `{"end": h}`; `h == sha256(line1)`; line 1 parses to a dict with `file_type == "log"` and an int
`format_version >= 1`. Otherwise it returns None (unfinished or damaged). If `format_version` is
above `FORMAT`, it raises `FormatTooNew`. Both lines are valid JSON, so the file also reads as JSON
Lines in other tools.

Example: `logs/hx-events/2026-10/20261029T120005123Z.lock_taken_over.pr_9c8b7a6d5e.4be1a9.json`

```
{"format_version":1,"file_type":"log","log_kind":"hx-events","logged_at":1793275205.123,"logged_by":"EXAMPLE\\jsmith","logged_on":"FLOOR01","logged_pid":4242,"type":"lock_taken_over","printer_id":"pr_9c8b7a6d5e","previous":{"token":"5be0c41d93aa7f20","user":"EXAMPLE\\akim","computer":"FLOOR02","purpose":"send","counter":7},"unchanged_s":60.4}
{"end":"9df18f692105394cab4ba02654291492519cdcf0f314fabb26f0ed26b1169ba7"}
```

### 6.5 Writing (`ShareStore.put_log_bytes(kind, name, raw) -> str`)

1. `rel = f"logs/{kind}/{log_month(name)}/{name}"`. Call `create_exclusive(rel, raw)`, which writes
   the whole file through the handle that created it (add-only folders allow nothing else).
2. If that raises `FileNotFoundError`, the month folder is missing: `makedirs` it and repeat step 1
   once. If creating the folder is refused, raise `ShareDenied`.
3. If the file already exists, read it. If its bytes equal `raw`, return `name`: the entry was
   uploaded before, but its marker wasn't written. Otherwise try the same name with `-2` before
   `.json`, then `-3`, up to `-9`, and return the first that succeeds. If all are taken, raise
   `ShareError`.
4. If a write fails after the file was created, the file stays unfinished under that name forever and
   readers skip it. The caller queues the entry (§13.6), and the upload later lands under `-2`.

`ShareStore.append_log(kind, entry) -> str` validates the entry, builds `stored`, `name` and `raw`, and
calls `put_log_bytes`. It raises on any failure and never queues. Application code logs through
`DataService.append_log` (§13.6), which queues when the share can't be written, so no entry is lost.

### 6.6 Reading

| Method | Returns |
|---|---|
| `list_logs(kind, *, since=None, entry_type="", ref="")` | Names, sorted. Month folders are listed from `log_month` of `since` up to the current UTC month + 1 (to allow another PC's clock to be slightly ahead), or every month folder when `since` is None. A name is kept when it matches the pattern, its stamp is ≥ the stamp of `since`, and its type and ref parts equal `entry_type` and `ref` when those are given. No file is opened. |
| `read_log(kind, name)` | The stored dict with `"log_name": name` added, or None if the file is missing, unfinished, damaged or from a newer format |
| `read_logs(kind, *, since=None)` | `read_log` of every name from `list_logs(kind, since=since)`, skipping the Nones |

Reading costs about 1–3 ms per file over SMB. Callers narrow down with `list_logs` filters and
`since` instead of reading whole months. Each listing and each read is one share operation, with the
5 s timeout.

### 6.7 Entry types

storage.md owns these types:

| type | Log | Written by | Fields besides the envelope |
|---|---|---|---|
| `record_saved` | hx-admin | `DataService.save` | `record_type` (file type), `record_id`, `revision`, `previous_revision` (int or null), `record` (dict), `previous` (dict or null). Printer tokens appear as `"(set)"` or `""` |
| `file_added` | hx-admin | `DataService.save_file` | `film_id`, `kind`, `name`, `path`, `sha256`, `size` |
| `lock_taken_over` | hx-events | `PrinterLock.acquire` | `printer_id`, `previous` {`token`, `user`, `computer`, `purpose`, `run_id`, `counter`}, `unchanged_s` |

send.md owns the Send, switch, confirmation, Leave it, Stop, Resume, Set clocks, test-print and
calibration types. It must use the type `send` for the one main entry of each Send, because
`DataService.find_send` looks for that type. states.md owns the poller's types: state changes, alerts,
the daylight-saving clock set, daily-check changes and internal errors.

## 7. PC files

All PC files are written whole and swapped in through a plain `FileOps`: local disk, no `hx-io`
timeout. `LocalStore` creates its folders on first use, and is thread-safe through one lock.

### 7.1 Per-user settings: `<roaming>/maplejet.json` and `<roaming>/settings.json`

1. `read_user_json(name)` reads `<roaming>/<name>` with `file_type` `user_settings` and returns the
   dict without the envelope keys. It returns `{}` if the file is missing, damaged or from a newer
   format.
2. `write_user_json(name, data)` adds the envelope and writes the file whole.
3. `maplejet.json` holds `HxSettings` (ui.md owns the fields). Example:
   `{"format_version": 1, "file_type": "user_settings", "floor_board_at_startup": true,
   "floor_board_screen": 1, "floor_board_size": "full"}`.
4. `settings.json` stays in today's Zebra format, without an envelope. `stripes.py` reads and writes
   it as it does today, at its new path, after `copy_once` from the old path.
5. `%APPDATA%` is local or served from the Offline Files cache, so `read_user_json` may be called on
   the Tk thread at start-up. Every other `LocalStore` call happens on worker threads.

### 7.2 The local copy: `<local>/hx/local-copy.json`

`file_type` `local_copy`. It is one file, written whole and swapped in, so it is always one consistent
set. `write_local_copy(data)` writes it; `read_local_copy()` returns an `HxData` with `source="local"`,
`loaded_at` = now and `reached_at` from the file, or None if the file is missing, damaged, from a
newer format, or made from a different share.

| Key | Type | Content |
|---|---|---|
| `written_at` | float | when written |
| `share_root` | str or null | `HxPaths.share_root` it came from. `read_local_copy` returns None if this differs from the current one |
| `reached_at` | float | `HxData.reached_at`: the last time this PC confirmed the data was current on the share (§13.3) |
| `loaded_at` | float | when the data was last loaded in full |
| `changes_counter` | int or null | `changes.json` counter of that load |
| `company` | object | `to_json(Company)` |
| `lines`, `machines`, `printers`, `adjustments`, `calibrations`, `products` | object | every record, keyed by id, each `to_json(record)` without its own envelope |
| `films`, `print_texts` | object | only those with `status == "active"`, keyed by id |

Example (shortened):

```json
{
 "format_version": 1,
 "file_type": "local_copy",
 "written_at": 1793275260.0,
 "share_root": "\\\\SERVER\\Share\\Stripes\\v2",
 "reached_at": 1793275260.0,
 "loaded_at": 1793271660.0,
 "changes_counter": 57,
 "company": {"id": "company", "revision": 3, "time_server": "time.example.local", "...": "..."},
 "lines": {"ln_1a2b3c4d5e": {"id": "ln_1a2b3c4d5e", "name": "Line 1", "...": "..."}},
 "machines": {"mc_5d1e2f3a4b": {"id": "mc_5d1e2f3a4b", "name": "Line 1 Bagger 1", "...": "..."}},
 "printers": {"pr_9c8b7a6d5e": {"id": "pr_9c8b7a6d5e", "host": "192.0.2.10", "uid": 1109, "...": "..."}},
 "adjustments": {},
 "calibrations": {},
 "products": {"pd_7e6f5a4b3c": {"id": "pd_7e6f5a4b3c", "name": "Example Co 2lb Onion", "...": "..."}},
 "films": {"fm_3f9a1c22b0": {"id": "fm_3f9a1c22b0", "status": "active", "...": "..."}},
 "print_texts": {"pt_0b1c2d3e4f": {"id": "pt_0b1c2d3e4f", "status": "active", "...": "..."}}
}
```

(`"...": "..."` marks fields left out of this example. Real files contain every field.)

### 7.3 Photo previews: `<local>/hx/previews/<sha256>.png`

These are content-addressed copies of `templates/films/<film_id>/previews/<sha256>.png` for the photos
of active films. Because they are keyed by content, copying them before swapping in the local copy
keeps the set consistent. `prune_previews(keep)` deletes every PNG whose sha256 isn't in `keep`; it
runs after each local-copy write. Previews exist only for image formats Stripes already handles
(the plan). Other originals open in the PC's default viewer.

### 7.4 Reach memory and poller memory: `<local>/hx/reach.json`, `<local>/hx/poller-*.json`

`reach.json` (`file_type` `local_state`) is `{"last_printer_reach": <float or null>}`, kept by
`ReachMemory` (§12.3). states.md stores the poller's own memory, such as daily checks and the last
daylight-saving clock set, in `poller-<name>.json` files through `read_local_json` and
`write_local_json`. Each file has a single writer.

### 7.5 The upload queue: `<machine>/send-queue/`

1. Log entries that couldn't be written to the share wait here, for any `LOG_KINDS`. The folder is
   shared by every user of the PC.
2. Each entry is one file, `<kind>~<log name>`. Its bytes are exactly `encode_log(stored)`, the same
   bytes that will go to the share. It is created with `create_exclusive`.
3. An uploaded entry gets a marker, `<kind>~<log name>.uploaded`, created with `create_exclusive`
   (`file_type` `upload_marker`, fields `by`, `computer`, `at`). It is a separate file because the
   uploader may be a different user, who may not change or delete someone else's file. After
   marking, the uploader deletes the entry and the marker where Windows lets it, which is normally
   only its own. Leftover markers are harmless.
4. `queued_logs()` returns `(kind, name, stored)` for every complete entry without a marker, oldest
   first. Unfinished entries are skipped.
5. The default permissions of `%PROGRAMDATA%` let every user create files in a subfolder and read
   other users' files, but not change or delete them. If creating the folder or a file there raises
   `PermissionError`, this user's entries go to `<local>/hx/send-queue/` instead (same format), and
   `queued_logs()` lists both folders. OPEN: confirm `%PROGRAMDATA%` permissions on the floor-PC
   image.
6. `queue_send_log(name, stored)` is `queue_log("hx-sends", name, stored)`. `pending_send_logs()` is
   the hx-sends part of `queued_logs()` as `(name, stored)`. `mark_uploaded(name)` marks the queued
   file whose log name is `name`, whatever its kind (log names are unique).

## 8. Whole-file writes and swap-safe reads (FileOps)

### 8.1 Operations

`FileOps` holds no state apart from `tmp_tag`, which `ShareStore` sets to
`sha256(f"{computer}|{user}")[:12] + "-" + pid`. Paths are absolute.

| Operation | Behaviour |
|---|---|
| `read_bytes(path)` | `hx_win.open_for_read(path)` (FILE_SHARE_READ, WRITE and DELETE), read everything, close at once. On `PermissionError` (a sharing violation from a program that doesn't share delete) it retries after 0.05, 0.1 and 0.2 s (`READ_RETRY_S`), then raises. `FileNotFoundError` propagates. A handle is never kept open. |
| `write_whole(path, data)` | 1) `tmp = f"{path}.{tmp_tag}-{secrets.token_hex(3)}.tmp"`. 2) `open(tmp, "xb")`, write, `flush`, `os.fsync`, close. 3) `os.replace(tmp, path)`; on `PermissionError` it retries after 0.05, 0.1, 0.2, 0.4, 0.8 and 1.6 s (`REPLACE_RETRY_S`, about 3.2 s in all, inside the 5 s share timeout). 4) On final failure it removes `tmp` (best effort) and raises. It never creates parent folders. |
| `create_exclusive(path, data)` | `os.open(path, O_WRONLY | O_CREAT | O_EXCL | O_BINARY)` (CREATE_NEW: atomic on the server, so exactly one PC wins). On `FileExistsError` it returns False. Otherwise it writes all of `data` through that same descriptor, then `os.fsync`, closes, and returns True. If anything fails after the create, it closes and raises, leaving the partial file. |
| `rename(src, dst)` | Windows: `os.rename` (fails if `dst` exists). Elsewhere: `os.link(src, dst)` then `os.unlink(src)`, so an existing `dst` also fails. Returns False on `FileNotFoundError` (src gone) or `FileExistsError` (dst exists). Never replaces. |
| `listdir(path)` | `os.listdir`. `FileNotFoundError` propagates. |
| `makedirs(path)` | `os.makedirs(path, exist_ok=True)` |
| `remove(path)` | `os.remove`. A missing file isn't an error. |
| `exists(path)` | Opens with `open_for_read` and closes. True if that works or raises `PermissionError`, False on `FileNotFoundError`. It never uses `os.path.exists` or `os.stat`. |

The remaining file rules:

1. Readers of whole-file files skip `*.tmp`.
2. Cleaning up temp files: once, after a `ShareStore` first reaches the share, it lists
   `status/machines`, `status/lines`, `status/locks` and `status/pollers` and removes `*.tmp` files
   whose tag has its own 12-hex prefix but another pid. These were left by an earlier crashed run of
   this user on this PC; `SingleInstance` means no other live process has that prefix.

### 8.2 Windows notes

1. **Open, don't stat.** `os.path.exists`, `isfile`, `getmtime` and `os.stat` are answered from the
   SMB client's caches: file information for up to 10 s (`FileInfoCacheLifetime`) and "not found" for
   up to 5 s (`FileNotFoundCacheLifetime`). Directory listings can be up to 10 s old
   (`DirectoryCacheLifetime`), so a new file can be missing from a listing. Locks, acks and status
   files are therefore always opened by name, and a lock is never judged absent from a listing.
   Listings are used only for collections: records, heartbeats, logs and the queue.
2. **Even an open by name can say "not found" for up to 5 s** after another PC created the file,
   because of the not-found cache. The lock protocol is safe whatever the delay: a sender never makes
   a request until the assigned poller has acknowledged its lock (§11.6). The two extra rules there
   (a missing poll file counts only after `stale_s`; a starting poller settles before its first
   request) close the gaps. OPEN: measure the delay on the real share.
3. **Swaps.** Readers hold a handle only for the milliseconds of one read, and always with
   FILE_SHARE_DELETE (`hx_win.open_for_read`). Writers retry `os.replace` for about 3 s, which covers
   antivirus scanners, backup agents, Explorer's preview and Notepad. Whether the file server lets a
   replace through while another PC holds the target open with FILE_SHARE_DELETE is OPEN
   (Windows-only test, §17.2). The design doesn't depend on it, because handles are held so briefly.
4. **Add-only folders** can't use write-then-rename, because a rename needs delete rights. A log file
   is created and written through one handle (`create_exclusive`), and the completion marker tells
   readers it is finished.
5. **One server, no offline caching.** v2 must be on one file server, not replicated with DFS-R. A
   DFS namespace with a single target is fine. The share must have offline caching off
   (`Set-SmbShare -CachingMode None`), or Offline Files can serve stale copies. These are IT
   checks (§14.3).
6. **Clocks.** Decisions never use file times or another PC's timestamps (§10). The only use of
   wall-clock times across PCs is the `since` filter for logs, which affects display only.
7. **Paths.** Use the UNC path from `ConfigPath`, never a mapped drive. Keep under 260 characters
   (§2). Names are compared case-sensitively by Stripes, but every name it makes is unique even
   ignoring case.
8. **Encoding.** UTF-8 everywhere. PowerShell 5.1's `Set-Content -Encoding UTF8` writes a BOM, which
   readers accept (§0.3).

## 9. ShareStore: timeouts and share health

### 9.1 The hx-io pool

1. Every share operation is one `FileOps` call, run on an `hx-io-<n>` daemon thread. The caller waits
   with `threading.Event.wait(timings.share_timeout_s)`. This is a real-time wait, because it guards
   against OS calls that hang, so tests use scaled timings rather than `FakeClock`.
2. An operation starts at once: on an idle worker if there is one, or on a new thread. Up to 4 idle
   workers are kept, and at most 16 threads live at a time, abandoned ones included (`POOL_MAX`). If
   all 16 are busy, the call raises `ShareUnavailable` at once.
3. On timeout the caller gets `ShareUnavailable(Msg(N_("The server didn't answer within {seconds}
   s."), {"seconds": 5}))`. The thread is abandoned: it finishes when its OS call returns, then exits
   instead of rejoining the pool.
4. Large copies (`copy_in`, §13.5) run as one operation per 1 MiB chunk (`CHUNK_BYTES`), each with
   the 5 s timeout, keeping the same file object across chunks. A big file never needs a longer
   timeout.

### 9.2 Errors

| Raised by the operation | The store raises | Share marked down? |
|---|---|---|
| timeout (§9.1) | `ShareUnavailable` | yes |
| `OSError` whose `winerror` is in `NETWORK_WINERRORS` = {51, 53, 54, 59, 64, 67, 121, 1222, 1231, 1232, 1236, 2250}, or whose `errno` is in {`ENETUNREACH`, `EHOSTUNREACH`, `EHOSTDOWN`, `ETIMEDOUT`, `ECONNRESET`, `ENOTCONN`, `ESTALE`} | `ShareUnavailable(Msg(N_("Stripes can't reach the server.")))` | yes |
| `FileNotFoundError` on a read | the read returns None | no |
| `FileNotFoundError` on a write (folder missing) | `ShareError(Msg(N_("Stripes can't find {folder} on the server."), {"folder": <rel folder>}))` | no |
| `PermissionError` | `ShareDenied(Msg(N_("Stripes isn't allowed to change {folder} on the server."), ...))` | no |
| any other `OSError` | `ShareError(Msg(N_("Stripes couldn't use {folder} on the server: {error}"), ...))` | no |

`ShareDenied` is a new subclass of `ShareError` in `hx_share` (§15).

### 9.3 Down, probe and recovery

1. `available` starts True and `last_reached` starts None. `last_reached = clock.time()` after every
   successful operation.
2. When the share is marked down, the store sets `available = False` and `down_reason` (the `Msg`).
   From then on every call raises `ShareUnavailable` at once without touching `FileOps`. The window
   and the threads never wait on a dead server.
3. **Probe.** When a call arrives while the share is down, and at least `share_retry_s` (30 s,
   measured with `clock.monotonic()`) have passed since the last probe started, the store starts a
   probe on an `hx-io` thread and still fails the call at once. The probe is `listdir(root)` with
   the 5 s timeout. On success: `available = True`, `down_reason = None`, `last_reached` is updated,
   and the next calls go through. The watcher and poller call the store every few seconds, so in
   practice the share is probed every 30 s while it is down. `probe_now()` starts a probe straight
   away, for a Retry button.
4. If `root` is None (no share configured), `available` is always False, `down_reason` is
   `Msg(N_("No server folder is set up for Stripes on this PC."))`, and no probe ever runs.
5. A missing root (`FileNotFoundError` from the probe's listing) counts as down, with the reason
   "Stripes can't find {folder} on the server."

### 9.4 Status method rules

| Method | Rule |
|---|---|
| `read_json(rel)` | `decode_json` with any `file_type`. None if the file is missing. Raises `FormatTooNew` or `ValueError` (damaged) |
| `write_json(rel, data)` | `data` must contain `file_type`. `encode_json` then `write_whole` |
| `read_poll`, `read_expected`, `read_line_status` | Update the watch (§10). Return None if missing or damaged. Raise `FormatTooNew` after updating the watch from the stable `counter` |
| `write_poll` | §5.2 |
| `write_expected`, `write_line_status` | §5.4, §5.5 |
| `read_ack`, `write_ack` | §5.6 |
| `read_preview`, `write_preview` | §5.3. `write_preview` refuses more than 1,000,000 bytes (`ValueError`) |
| `read_lock`, `lock` | §11 |
| `heartbeat`, `live_heartbeats` | §12 |

Every ID passed to these methods is checked against §2. A bad one raises `ValueError`.

## 10. Freshness (CounterWatch)

`CounterWatch` decides freshness from change, on this PC's own `clock.monotonic()`. It is
thread-safe (one lock), and one instance (`ShareStore.watch`) is shared by everything that reads
status files through that store.

| Method | Behaviour |
|---|---|
| `see(key, counter) -> float` | If the key is new, or was last seen missing, or `counter` differs from the last value seen: record (counter, now) and return 0.0. Otherwise return now − the time it last changed. |
| `age(key) -> float \| None` | The same age without a new reading. If the key is currently missing, the time since it was first seen missing. None for a key never seen. |
| `missing(key) -> float` | Marks the key as missing. Returns the seconds it has been missing without a break (0.0 the first time). Seeing the key again resets this. |
| `forget(key)` | Drops the key |

Keys are `poll:<machine_id>`, `expected:<machine_id>`, `line:<line_id>`,
`lock:<printer_id>:<token>` (with `lock:<printer_id>` for "no lock file") and `hb:<heartbeat key>`.
A damaged lock file is seen with the counter `zlib.crc32(raw bytes)`. Keys untouched for 3,600 s are
pruned on the next `see`.

Rules:

1. A counter seen for the first time is fresh (age 0). So a PC that starts while a holder has
   crashed waits the full `lock_stale_s` before taking the lock (architecture.md §7.4).
2. "Missing" counts as stale only once it has been missing for the stale time on this PC's own clock.
   It is never stale just because one open said "not found" (§8.2 note 2).
3. `ShareStore.poll_age(machine_id)` is `watch.age("poll:" + machine_id)`. `lock_age(printer_id)` is
   the age of the current token's key, or None when the last read found no lock.

## 11. Printer locks

### 11.1 States

`ShareStore.lock(printer_id, *, purpose, run_id, line_id="", machine_id="")` returns an unheld
`PrinterLock`. `acquire()` and `start_keeping()` are called on one thread (the one that creates the
lock). `check()`, `update()` and `release()` may be called from any thread: send.md takes every lock
of a Send on its whole-Send thread and then uses each one from that machine's thread. One internal
`threading.Lock` guards `state`, `info`, `last_ok` and the pending note; the keeper thread takes it too.
No share operation runs while holding it, so `check()` never blocks on the share.

| `state` | Meaning |
|---|---|
| `unheld` | Not acquired yet, or released |
| `held` | Our lock file is on the share, with our token |
| `local_only` | The share was unreachable at acquire time. Only `store.local_locks` protects the printer, against this PC's own poller. Results are amber |
| `lost` | Our file is gone or names another token, or refreshing has failed for `lock_stale_s / 2` (30 s). `check()` raises `LockLost` |

### 11.2 Acquire

`acquire() -> str`:

1. If `printer_id` is already in `store.local_locks`, raise `LockHeld(Msg(N_("{user} on {computer} is
   using this printer."), {"user": info.user, "computer": info.computer}), info=info)` with `info` the
   holder's `LockInfo`. Another Send or test in this process holds it. Every `LockHeld` raised in
   §11.2 and §11.4 carries this same `Msg`, filled from its own `info`.
2. Register `store.local_locks[printer_id] = run_id`, together with the `LockInfo` kept privately in
   the store. This comes before any share access, so this PC's poller steps aside at once.
3. Build a `LockInfo` with a new `token`, our `user`, `computer` and `pid`, the purpose and ids,
   `counter` 1, `acquired_at = clock.time()` and an empty `note`.
4. `create_exclusive("status/locks/<printer_id>.lock", encode_json("lock", ...))`. On success, the
   state is `held`; return `"held"`.
5. `ShareUnavailable` at any point: the state is `local_only`; return `"local_only"`. The
   registration stays.
6. If the file exists, `read_lock(printer_id)`, which updates the watch. If the file has vanished in
   the meantime, go back to step 4 (at most 3 rounds in all). If the age of its counter is below
   `lock_stale_s`, remove the registration and raise `LockHeld(info)`. If the age is at or above
   `lock_stale_s`, take it over (§11.4).
7. Any other `ShareError`: remove the registration and raise it.

Because of rule 1 in §10, the first time this store sees a lock, its age is 0 and the result is
`LockHeld`. The status watcher reads every printer's lock every `watch_s`, so by the time someone
retries, the age is known.

### 11.3 Keeping the lock

`start_keeping()` starts `hx-lock-<printer_id>`. Every `lock_refresh_s` it does this:

1. Read the lock file by name.
2. If the file is missing, or holds another token: the state becomes `lost`, and the keeper stops
   without writing.
3. Otherwise set `counter += 1`, merge any note given to `update(**note)` since the last refresh, and
   `write_bytes` the whole file. On success, record `last_ok = clock.monotonic()`.
4. On `ShareError` or `ShareUnavailable`: if `clock.monotonic() − last_ok >= lock_stale_s / 2`, the
   state becomes `lost` and the keeper never writes again. Another PC can only take over after 60 s
   without change on its own clock, and this rule stops a holder that lost the share from ever
   recreating its file after a takeover.

For a `local_only` lock the keeper does nothing.

`last_ok` starts at the moment `acquire()` created or took over the file, and moves on every
successful refresh (step 3).

`check(budget_s: float = 0.0)` raises `LockLost(Msg(N_("Another PC took over this printer's lock.")))`
when the state is `lost`, or when the state is `held` and `clock.monotonic() − last_ok + budget_s >=
lock_stale_s / 2`, whatever the keeper has set so far (the keeper runs only every `lock_refresh_s`, so
it can notice a failing share up to 5 s late). In the second case the state also becomes `lost`. It
does nothing for a `local_only` lock. Holders call it before every printer request with `budget_s` =
the worst case of that call (`hx_protocol.worst_case_s`, protocol.md §8.3), so no request can still be
running when another PC may judge the lock stale: the last request starts at least `budget_s` before
`last_ok + lock_stale_s / 2`, and other PCs wait `lock_stale_s` after the last refresh they saw. A
holder that gets `LockLost` stops talking to that printer at once (send.md decides what is shown).

### 11.4 Takeover of a stale lock

The lock file X has token T, and its counter has been unchanged for at least `lock_stale_s` on this
store's watch.

1. `aside = X + ".stale-" + <our new token>`. If `rename(X, aside)` returns False, someone else moved
   or removed it: go back to §11.2 step 4.
2. Read `aside`. If its token isn't T, we have just moved someone else's new lock aside. Rename it
   back with `rename(aside, X)` (best effort) and raise `LockHeld` with its info. That holder notices
   at its next refresh if the rename back failed, and stops.
3. `create_exclusive(X, ours)`. If it returns False (a third PC got in first), raise `LockHeld` with
   that lock's info.
4. Remove `aside` (best effort). Write `lock_taken_over` to hx-events (§6.7) with
   `ShareStore.append_log`. If that fails, print to stderr and carry on.
5. The state is `held`; return `"held"`.

Only one rename of X can succeed, so of several PCs taking over at the same moment exactly one ends up
holding the lock.

### 11.5 Release

`release()` is idempotent.

1. Stop the keeper and join it, waiting at most `lock_refresh_s`.
2. If the state is `held`: read X. If it still has our token, remove it. If the removal fails
   (`ShareError` or `ShareUnavailable`), record the token in the store's pending releases. The probe
   (§9.3) and every later successful operation retry those removals, for up to 10 minutes, and only
   while the file still holds that token. Until the file is gone, this store's `read_lock` treats a
   file holding a released token as no lock, so this PC's poller resumes at once. Other PCs see it go
   stale after 60 s.
3. If the state is `lost`, the file isn't touched.
4. Remove the `local_locks` entry and the private `LockInfo`. The state becomes `unheld`.

`__enter__` calls `acquire()` and then `start_keeping()`; `__exit__` calls `release()`.

### 11.6 The poller handshake, file by file

Poller, on whatever PC, before every single request to a printer:

1. If `printer_id` is in `store.local_locks`, make no request. Write `paused_for_lock` = the token
   from `read_lock`, or `"local"` if there is none, in `poll.json` at once.
2. Otherwise `read_lock(printer_id)`. If there is a lock whose `lock_age` is below `lock_stale_s`,
   make no request, and write `paused_for_lock = token` in `poll.json` at once (if it isn't already
   there).
3. If there is no lock, or only a stale one: request as normal, with `paused_for_lock = ""` on the
   next write. What a stale lock means for the machine's state is states.md's.

Lock holder (sender, tester, Stop/Resume/Set clocks), before its first request to a printer:

1. A `local_only` lock: no handshake is possible, so go on. The in-process gate and `local_locks`
   still protect against this PC's own poller.
2. Otherwise call `store.check_poller_paused(machine_id, token)` every 0.25 s (send.md owns the loop
   and its time limit):

   | Result | Meaning | Next |
   |---|---|---|
   | `"paused"` | `poll.json` shows `paused_for_lock == token`, or its `poller` is this store's own `identity.key` (this PC's poller already steps aside through `local_locks`) | go |
   | `"stale"` | `poll.json`'s counter unchanged for `stale_s` | call again at once: go if the result is still `"stale"`, `"missing"` or `"paused"`; keep waiting if it is now `"polling"` |
   | `"missing"` | no `poll.json`, or a damaged one, for at least `stale_s` without a break | as `"stale"` |
   | `"polling"` | anything else | keep waiting |

   A newer-format `poll.json` is judged from its stable keys (`counter`, `poller`,
   `paused_for_lock`).

Poller starting to poll a printer (a new assignment, a takeover, a handover, or the app starting):

1. Write `poll.json` naming itself first, with the counter = the file's counter + 1.
2. Read the lock. Make the first request only after a lock read taken at least `settle_s()` (0.4 ×
   `stale_s` = 6 s) after that write still shows no lock.

Why both rules: Windows may report a just-created lock as "not found" for up to 5 s (§8.2). The
settle rule means a lock created before a starting poller's first request is seen before that request.
The re-check means a sender that saw "stale" or "missing" notices a poller that has just started.
Resuming after a release needs no settle, because the poller's `poll.json` stays fresh, so a new
holder waits for its own token.

Sequence of a normal Send (architecture.md §7.3, with file names):

1. Sender: `acquire()` creates `status/locks/pr_9c8b7a6d5e.lock` (token `a3f9c2e17b4d8e01`), then
   `start_keeping()`.
2. Poller (FLOOR02): before its next request it opens the lock file, sees it, makes no request, and
   writes `status/machines/mc_5d1e2f3a4b.poll.json` with `"paused_for_lock": "a3f9c2e17b4d8e01"`.
3. Sender: `check_poller_paused("mc_5d1e2f3a4b", "a3f9c2e17b4d8e01")` returns `"paused"`, and it
   sends its first request.
4. Sender: `expected.json` and `lines/ln_1a2b3c4d5e.json` written under the lock, the log entries
   written, `release()` removes the lock file.
5. Poller: its next lock read finds no lock. It polls again and writes `"paused_for_lock": ""`.

### 11.7 Reading locks

`read_lock(printer_id) -> LockInfo | None` opens `status/locks/<printer_id>.lock` by name.

| File | Result |
|---|---|
| missing | None. `watch.missing("lock:<id>")` |
| holds a token this store released (§11.5) | None |
| valid | its `LockInfo`. `watch.see("lock:<id>:<token>", counter)` |
| newer format | a `LockInfo` built from the stable keys (§3.4), still treated as a lock |
| damaged | `LockInfo(printer_id=..., token=f"damaged-{crc:08x}", user="", computer="", pid=0, purpose="unknown", counter=crc)`, where `crc = zlib.crc32(raw)`. It stays a lock until it has been unchanged for `lock_stale_s`, then it is taken over |

## 12. Heartbeats and poller assignment

### 12.1 Heartbeats

1. `store.heartbeat()` returns this store's single `Heartbeat`. Its `key` is `identity.key`.
2. `beat(can_poll=, last_printer_reach=, polling=, monitor_open=, floor_board=)` writes
   `status/pollers/<key>.json` whole, with `counter` + 1 (starting at 1 per process),
   `written_at = clock.time()` and `version = store.version`. The poller calls it every
   `heartbeat_s`. The store keeps its last `HeartbeatInfo` in memory.
3. `remove()` deletes the file on a clean exit. It is best effort, and stale files are cleaned up
   anyway (§12.2).

### 12.2 The live set

`live_heartbeats() -> list[HeartbeatInfo]`:

1. List `status/pollers/`. For every name matching `<key>.json`, read the file and skip it if it is
   damaged, of another `file_type`, or has `format_version != FORMAT` (exact equality, plan:
   "pollers count only heartbeats of their own format").
2. A heartbeat is live when `watch.see("hb:" + key, counter) < timings.stale_s`.
3. This store's own last heartbeat is always included from memory, because a listing may hide a new
   file for 10 s.
4. Clean-up: a file whose counter this store has seen unchanged for `40 × stale_s` (600 s) is
   removed, best effort. A live PC whose file was removed recreates it at its next beat.
5. The result is sorted by key.

### 12.3 Can poll: reach memory and the reachability probe

A PC is given printers only if it reached any printer within `printer_reach_window_s` (one day). This
keeps office PCs that can't reach the printer network out of the pool, and a night with every machine
off doesn't drop good PCs.

`ReachMemory` keeps the last reach in `<local>/hx/reach.json`, so it survives restarts:

1. `reached()` sets `last_printer_reach = clock.time()`. The Poller calls it after every successful
   printer reply and every successful probe. The file is written when the value goes from None to a
   time, or when the stored value is at least 300 s older.
2. `can_poll()` is True when `last_printer_reach` is set and
   `-300 <= clock.time() − last_printer_reach <= printer_reach_window_s`. Wall-clock time is used
   because it must survive restarts; the 300 s allows for small clock corrections.
3. `probe_due(unpolled=...)` and `probed(ok)` decide when the Poller probes:

   | Situation | Probe due when |
   |---|---|
   | some active printer has no live poller (`unpolled=True`: its `poll.json` stale or missing for `stale_s`) | no probe in this process yet, or the last probe started at least `4 × stale_s` (60 s) ago |
   | otherwise | never: a printer someone is polling is never probed |

4. **The probe** (the Poller, hx_poller, does it, following these rules exactly):
   - Target: an active printer whose `poll.json` is stale or missing for `stale_s` (no live poller),
     that has no lock (`read_lock` returns None) and isn't in `local_locks`; among several, take them
     in turn by printer id. No such printer: no probe.
   - Action: `clients.client(printer).probe()` (protocol.md §9 item 7): a bare TCP connect and close
     under that printer's gate, taken without blocking, sending nothing. It is a TCP connect, not an
     Autopilot request.
   - `True` calls `reached()` and `probed(True)`; `False` calls `probed(False)`; `None` (this PC's
     gate for that printer was busy, so nothing was sent) calls neither, and the probe is due again
     at the next heartbeat.
   - A PC whose reach has expired joins the pool through this probe the moment a printer loses its
     poller. Every PC that polls keeps its reach fresh, so takeovers between polling PCs aren't
     delayed; a PC that polls nothing drops out of the rankings a day after its last reply.
   - OPEN: confirm on the test unit that a bare connect doesn't disturb a printing printer
     (architecture.md §11).

### 12.4 The assignment formula (rendezvous hashing)

```python
def score(printer_id: str, computer: str) -> int:
    digest = hashlib.sha256(f"{printer_id}|{computer.upper()}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")

def assign(printer_ids, heartbeats):
    pollers = {}
    for hb in heartbeats:
        if hb.can_poll:
            pollers.setdefault(hb.key, hb)            # one entry per key
    return {pid: [hb.key for hb in sorted(pollers.values(),
                                          key=lambda hb: (-score(pid, hb.computer), hb.key))]
            for pid in printer_ids}
```

1. Rank 0 is the one that polls. Each printer's list is the takeover order.
2. Scores depend on the computer, not the user or process. A restarted Stripes on the same PC keeps
   its printers, and two users signed in on one PC sit next to each other in every ranking. The one
   with the lower key polls, and the other takes over on the same PC.
3. Removing a PC moves only the printers it was rank 0 for, each to its own next-ranked PC. Adding a
   PC takes only the printers it now scores highest for.
4. The callers pass `printer_ids` (states.md: active printers of active machines) and
   `live_heartbeats()`. Every PC gets the same answer from the same inputs.

Test vector: `score("pr_0000000001", "FLOOR01") == 5924401758749967213` (the first 8 bytes of
sha256 `5237b402c0fdc36d...`). Golden table for heartbeats from FLOOR01 to FLOOR04 (keys
`FLOOR0n_EXAMPLE_jsmith_10n`, all able to poll), printers `pr_0000000001` to `pr_000000000c`:

| Printer | Rank order | Rank order without FLOOR02 |
|---|---|---|
| pr_0000000001 | FLOOR03, FLOOR04, FLOOR01, FLOOR02 | FLOOR03, FLOOR04, FLOOR01 |
| pr_0000000002 | FLOOR03, FLOOR01, FLOOR02, FLOOR04 | FLOOR03, FLOOR01, FLOOR04 |
| pr_0000000003 | FLOOR03, FLOOR01, FLOOR02, FLOOR04 | FLOOR03, FLOOR01, FLOOR04 |
| pr_0000000004 | FLOOR02, FLOOR04, FLOOR03, FLOOR01 | FLOOR04, FLOOR03, FLOOR01 |
| pr_0000000005 | FLOOR03, FLOOR02, FLOOR04, FLOOR01 | FLOOR03, FLOOR04, FLOOR01 |
| pr_0000000006 | FLOOR03, FLOOR04, FLOOR02, FLOOR01 | FLOOR03, FLOOR04, FLOOR01 |
| pr_0000000007 | FLOOR01, FLOOR03, FLOOR04, FLOOR02 | FLOOR01, FLOOR03, FLOOR04 |
| pr_0000000008 | FLOOR03, FLOOR04, FLOOR02, FLOOR01 | FLOOR03, FLOOR04, FLOOR01 |
| pr_0000000009 | FLOOR03, FLOOR01, FLOOR04, FLOOR02 | FLOOR03, FLOOR01, FLOOR04 |
| pr_000000000a | FLOOR04, FLOOR03, FLOOR02, FLOOR01 | FLOOR04, FLOOR03, FLOOR01 |
| pr_000000000b | FLOOR02, FLOOR04, FLOOR01, FLOOR03 | FLOOR04, FLOOR01, FLOOR03 |
| pr_000000000c | FLOOR02, FLOOR01, FLOOR03, FLOOR04 | FLOOR01, FLOOR03, FLOOR04 |

With few printers the split is uneven (FLOOR03 gets 7 of 12). That is within one PC's capacity
(architecture.md decision 2). Over 1,000 printers (`pr_{i:010x}`, i = 0..999) and five PCs
(FLOOR01 to FLOOR05) the rank-0 counts are 213, 206, 199, 197 and 185.

### 12.5 Takeover order

`takeover_after_s(rank, timings) -> float`: rank 0 gives 0.0; rank 1 gives `takeover_s[0]` (15 s);
rank 2 gives `takeover_s[1]` (20 s); rank n > 2 gives `takeover_s[1] + (takeover_s[1] −
takeover_s[0]) × (n − 2)` (25, 30, ... s). A PC at rank r ≥ 1 may start polling a printer once
`poll_age` (the counter unchanged, or the file missing, §10) has reached `takeover_after_s(r)`.

### 12.6 Starting and handing over: the file rules

These are the file-level rules behind architecture.md §7.3. states.md builds the poller on them.

1. **The invariant:** no PC starts polling a printer whose `poll.json` is fresh (`poll_age <
   stale_s`) and names another `poller`, unless that file's `yielded_to` names this PC's key.
2. Rank 0 starts when `poll.json` names itself as `poller`, names it in `yielded_to`, has been
   missing for `stale_s`, or has been stale for `stale_s`. Rank r ≥ 1 starts at
   `takeover_after_s(r)`.
3. Every start follows the settle rule of §11.6.
4. Handover: a poller that is no longer rank 0 while rank 0 is live finishes its request in flight,
   writes `poll.json` with `yielded_to = <rank 0 key>` (counter + 1), and stops polling that printer.
5. A cleanly stopping poller does the same towards its next-ranked live poller.
6. A poller re-checks its rank (from `live_heartbeats()` and `assign()`) before every cycle.
7. A poller whose `poll.json` writes or lock reads have failed for `stale_s / 2` stops share-mode
   polling of that printer (states.md switches it to standalone mode). Otherwise another PC could
   judge it stale and start while it is still talking to the printer.

## 13. DataService

### 13.1 Thread and cycle

`start()` starts the `hx-data` daemon thread:

1. First, `local.read_local_copy()`. If there is one, it becomes `current` (`source="local"`) and a
   `DataUpdate` is posted, so the UI has data within milliseconds even while the share is slow.
2. Then it loops every `watch_s`, waiting with `clock.sleep(watch_s, cancel)`, or sooner on
   `reload()`:
   1. If `store.available` changed since the last pass, post `ShareHealth(ok, store.last_reached)`.
      When it becomes False, replace `current` with `dataclasses.replace(current, source="local")`
      (the content stays; a Send now says "templates as of"). Post `DataUpdate`.
   2. Run a data cycle when `reload()` was called, `data_refresh_s` has passed since the last one, or
      the share has just become available again.
   3. After each data cycle, flush the queue (§13.6).
3. `run_once()` runs one data cycle and one flush on the calling thread, for tests.

### 13.2 Loading

A data cycle:

1. If `store.root` is None, the problem is "No server folder is set up for Stripes on this PC."; keep
   `current` (the local copy, if any) and stop.
2. Read `config/hx/changes.json`. `ShareUnavailable` ends the cycle: keep `current`, as in §13.1
   step 2.1.
3. A full reload is needed when `current` is None or not from the share, the counter is missing or
   differs from the last full load's, the previous cycle was a full reload triggered by a change (the
   listing may have hidden new files for 10 s), or `10 × data_refresh_s` has passed since the last
   full reload.
4. **No full reload needed:** `current = dataclasses.replace(current, reached_at=now, loaded_at=now)`.
   Rewrite the local copy only if its stored `reached_at` is at least 600 s old. Post no
   `DataUpdate`.
5. **Full reload:** list and read every record file: `config/hx/company.json`, `lines/`,
   `machines/`, `printers/`, `adjustments/`, `calibrations/`, `templates/products/`, each
   `templates/films/<id>/film.json`, and `templates/print-texts/`. For each file, `decode_json` with
   its `file_type`, then `from_json`, and check the id against the path.
   - `company.json` missing: the problem is "MapleJet isn't set up on the server yet." The load is
     incomplete.
   - A damaged or oversized file: the problem is `Msg(N_("{file} on the server is damaged."))`, and
     the load is incomplete.
   - A newer-format file: its rel path is added to `too_new`, the problem is the `FormatTooNew` msg,
     and the load is incomplete.
   - `ShareUnavailable`: the cycle ends as in step 2.
6. **Complete:** `current = HxData(..., source="share", loaded_at=now, reached_at=now)`; `problems =
   []`; `too_new = []`. Write the local copy and its previews (§13.3). Post `DataUpdate`.
7. **Incomplete:** keep `current` unchanged (an older complete set, or the local copy), without
   moving `reached_at`. Set `problems`, and post `DataUpdate(current)` if `problems` changed. Don't
   write the local copy.

A load isn't a snapshot: an admin save during the load can mix revisions. The save bumps
`changes.json`, so the next cycle reloads. Each file read is one share operation. A full reload of
about 1,000 files takes 1–3 s on the `hx-data` thread.

### 13.3 The local copy and the 24-hour rule

1. After every complete load: for each active film, for each FileRef whose preview isn't in the
   local cache (`local.has_preview(sha256)`), read `templates/films/<id>/previews/<sha256>.png` and
   `local.write_preview(sha256, png)` it (a missing preview is skipped). Then
   `local.write_local_copy(current)`, then `local.prune_previews(<sha256 of every active film's
   FileRef>)`.
2. `reached_at` is the last time this PC confirmed, against the share, that its data was current: a
   complete load, or a cycle that found `changes.json` unchanged (§13.2 step 4).
3. `can_send() -> tuple[bool, Msg | None]`, checked in this order:

   | Condition | Result |
   |---|---|
   | `current` is None | `(False, Msg(N_("The MapleJet settings haven't loaded yet.")))`, or the not-set-up problem when that's the reason |
   | `too_new` is not empty | `(False, <its FormatTooNew msg, "Update Stripes: ...">)` |
   | `age = clock.time() − current.reached_at` is within `-300 <= age <= local_copy_max_age_s` | `(True, None)` |
   | otherwise, the share unavailable | `(False, Msg(N_("Can't reach the server since {time}."), {"time": <reached_at>}))` |
   | otherwise, the share available (loads keep failing) | `(False, Msg(N_("Stripes hasn't been able to read the templates from the server since {time}."), ...))` |

   `{time}` is `time.strftime("%Y-%m-%d %H:%M", time.localtime(reached_at))`. A wall-clock time is
   used because the rule must survive restarts. A clock set back more than 300 s blocks sending
   until the share is reached again (the safe side). The UI's Retry button calls `store.probe_now()`
   and `reload()`.
4. A Send uses `current`. `SendPlan.from_local_copy = current.source == "local"`, and
   `data_as_of = current.reached_at` (send.md).

### 13.4 Saving records

`save(record, *, expected_revision) -> Record` is blocking (worker thread only):

1. `NotAllowed(Msg(N_("Only Stripes Administrators can change this.")))` unless
   `identity.is_admin(current.company)`.
2. `rel = record_rel(record)` (§2). Check the id pattern and the record's own ids: an adjustment's id
   equals `adjustment_id(machine_id, film_id)`, a calibration's id equals its `machine_id`, the
   company's id is `"company"`, and every FileRef path is inside its film's folder. A failure raises
   `ValueError`.
3. Read the current file (`read_json`). It raises `FormatTooNew` for a newer file, and stops there.
   - `expected_revision is None` (a new record): if the file exists, raise
     `RevisionConflict(Msg(N_("{user} changed this while you were editing it. Reload it and make
     your change again."), {"user": <the current file's updated_by>}), current=<its revision>)`.
   - Otherwise, if the file is missing or its `revision != expected_revision`, raise
     `RevisionConflict` the same way (a missing file gives `{"user": "?"}` and `current=0`).
4. Build the new content: `to_json(record)` with `revision` = the current revision + 1 (1 when new),
   `updated_by = identity.user` and `updated_at = clock.time()`. Add every top-level key of the
   current file that isn't an envelope key or a field of this class: keys a newer minor version
   added are kept.
5. `write_json(rel, ...)` (for a new film, `makedirs("templates/films/<id>")` first). Read it back. If
   the read-back's `revision`, `updated_by` and `updated_at` aren't ours, another administrator saved
   in the same moment: raise `RevisionConflict` with the read-back's revision and `{"user": <the
   read-back's updated_by>}`. The last write wins,
   and both saves are in the admin log.
6. Write `changes.json` with counter + 1 and `last` = this record.
7. `append_log("hx-admin", {"type": "record_saved", ...})` (§6.7). A printer's `token` in `record`
   and `previous` is written as `"(set)"` if non-empty.
8. A `Calibration` whose `head_distance_mm` is not None and differs from its machine's: also save the
   machine (`expected_revision` = its current revision) with `head_distance_mm` copied, going
   through steps 3 to 7.
9. `reload()`, then return the saved record (`from_json` of what was written).

There is no delete. Errors raised: `NotAllowed`, `RevisionConflict`, `FormatTooNew`,
`ShareUnavailable`, `ShareDenied`, `ShareError` and `ValueError`.

### 13.5 Film files and previews

`save_file(film_id, kind, source_path) -> FileRef` is blocking:

1. Administrator check, as in §13.4 step 1. `kind` must be one of `front_photo`, `back_photo`,
   `artwork`, `spec`, `other`.
2. A file over 50,000,000 bytes (`ATTACHMENT_MAX_BYTES`) raises
   `ShareError(Msg(N_("The file is too big (at most {mb} MB)."), {"mb": 50}))`.
3. Read the file locally to get its `sha256` and `size`.
4. `path = f"films/{film_id}/files/{sha256[:8]}_{safe name}"` (§2). If
   `templates/<path>` already exists and has the same size, don't copy again. Otherwise
   `makedirs("templates/films/<film_id>/files")` and `store.copy_in("templates/" + path,
   source_path)`.
5. `append_log("hx-admin", {"type": "file_added", ...})`.
6. Return `FileRef(kind=kind, name=os.path.basename(source_path), path=path, sha256=..., size=...,
   added_by=identity.user, added_at=clock.time())`. The caller adds it to the film and saves the film.

`ShareStore.copy_in(rel, source_path)` works as `write_whole`, but in 1 MiB chunks, one share
operation each (§9.1), then `fsync`, close and `os.replace` with the same retries.

`save_preview(film_id, sha256, png)`: administrator check; at most 512,000 bytes; must start with
the PNG signature `\x89PNG\r\n\x1a\n`. It is written whole to
`templates/films/<film_id>/previews/<sha256>.png`. The admin's film editor makes the PNG (ui.md) and
calls this right after `save_file` for image files.

`preview_bytes(film_id, sha256) -> bytes | None` (blocking) returns the local cache if present.
Otherwise it reads the share's preview and caches it with `local.write_preview`. None if neither has
it.

`share_path(rel) -> str | None` returns the absolute share path of a templates-relative or
share-relative path (None without a share), for opening an original in the default viewer from a
worker thread.

### 13.6 Logs and the queue

`append_log(kind, entry) -> tuple[str, bool]` is blocking:

1. Validate the entry (§6.2). `at = clock.time()`, `stored = make_log_record(kind, entry, identity,
   at)`, `name = log_name(entry, at)`, `raw = encode_log(stored)`.
2. If `store.available`, `store.put_log_bytes(kind, name, raw)`. On success, return
   `(final name, False)`.
3. On any `ShareError` (including `ShareUnavailable` and `ShareDenied`), or if the share isn't
   available: `local.queue_log(kind, name, stored)` and return `(name, True)`.
4. If the queue fails too (`OSError`), raise `ShareError(Msg(N_("Stripes couldn't save the log entry
   on this PC: {error}"), {"error": f"{type(e).__name__}: {e.strerror or e}"}))`.

`append_send_log(entry)` is `append_log("hx-sends", entry)`.

Flush, after each data cycle when `store.available` and `current` is set:

1. Take `local.queued_logs()`, oldest first, at most 200 per flush.
2. Upload only what this user is allowed to add (§14): `hx-events` always; `hx-sends` only if
   `identity.is_sender(company)`; `hx-admin` only if `identity.is_admin(company)`. Others stay
   queued.
3. `store.put_log_bytes(kind, name, encode_log(stored))`, then `local.mark_uploaded(name)`.
4. `ShareUnavailable` stops the flush. `ShareDenied` skips that kind until the next flush. Any other
   `ShareError` skips that entry.

`pending_logs() -> int` is `len(local.pending_send_logs())`, the "N log entries waiting" shown in the
Send window.

`find_send(send_id) -> dict | None` is blocking. It checks
`store.list_logs("hx-sends", since=now − 400 days, entry_type="send", ref=send_id)` and reads the
first complete one. If that finds nothing, or the share is unavailable, it searches the queue
(`local.queued_logs()` entries of kind hx-sends with `type == "send"` and `send_id == send_id`). None if
neither has it.

### 13.7 Problems and messages

`DataService.problems: list[Msg]` holds the last cycle's problems. `too_new: list[str]` holds the rel
paths of newer-format records. Both are replaced whole and readable from any thread. The UI shows
`problems` to administrators and in the Send window.

`hx_share` defines its Spanish texts and registers them with `i18n.add_spanish(ES)`:

| English (`N_`) | Spanish |
|---|---|
| The server didn't answer within {seconds} s. | El servidor no respondió en {seconds} s. |
| Stripes can't reach the server. | Stripes no puede conectarse con el servidor. |
| No server folder is set up for Stripes on this PC. | Esta PC no tiene una carpeta del servidor configurada para Stripes. |
| Stripes can't find {folder} on the server. | Stripes no encuentra {folder} en el servidor. |
| Stripes isn't allowed to change {folder} on the server. | Stripes no tiene permiso para modificar {folder} en el servidor. |
| Stripes couldn't use {folder} on the server: {error} | Stripes no pudo usar {folder} en el servidor: {error} |
| MapleJet isn't set up on the server yet. | MapleJet todavía no está configurado en el servidor. |
| {file} on the server is damaged. | {file} en el servidor está dañado. |
| Update Stripes: {file} was saved by a newer version. | Actualice Stripes: {file} se guardó con una versión más nueva. |
| The MapleJet settings haven't loaded yet. | La configuración de MapleJet todavía no se ha cargado. |
| Can't reach the server since {time}. | No se puede conectar con el servidor desde {time}. |
| Stripes hasn't been able to read the templates from the server since {time}. | Stripes no ha podido leer las plantillas del servidor desde {time}. |
| {user} on {computer} is using this printer. | {user} en {computer} está usando esta impresora. |
| Another PC took over this printer's lock. | Otra PC tomó el bloqueo de esta impresora. |
| {user} changed this while you were editing it. Reload it and make your change again. | {user} cambió esto mientras usted lo editaba. Vuelva a cargarlo y haga su cambio otra vez. |
| Only Stripes Administrators can change this. | Solo los Administradores de Stripes pueden cambiar esto. |
| The file is too big (at most {mb} MB). | El archivo es demasiado grande (máximo {mb} MB). |
| Stripes couldn't save the log entry on this PC: {error} | Stripes no pudo guardar el registro en esta PC: {error} |

## 14. Permissions and the folder script

### 14.1 Who can do what

This is the plan's table. Senders and administrators are also Domain Users, so their rights add up.

| Folder under `v2\` | Domain Users | Stripes MapleJet Senders | Stripes Administrators |
|---|---|---|---|
| `v2\` itself, `status\`, `logs\` (the folders only) | Read | Read | Read |
| `config\` (Zebra files and `hx\`) | Read | Read | Modify |
| `templates\` | Read | Read | Modify |
| `status\machines\`, `status\pollers\`, `status\deployment\` | Modify | Modify | Modify |
| `status\lines\`, `status\locks\`, `status\alerts\` | Read | Modify | Modify |
| `logs\hx-sends\` | Read | Read and add files | Read |
| `logs\hx-events\` | Read and add files | Read and add files | Read and add files |
| `logs\hx-admin\` | Read | Read | Read and add files |

What each right is used for:

| Who | Writes |
|---|---|
| Every PC (Domain Users) | `poll.json` and `preview.bmp` (status\machines), heartbeats, the Zebra deployment log, hx-events entries |
| Senders | expected states (status\machines), line statuses, locks, acks, hx-sends entries |
| Administrators | records, film files and previews, `changes.json`, expected states and locks during tests, acks, hx-admin entries |

Pollers only read `status\locks` and `status\alerts`. An administrator who also sends is put in both
groups (architecture.md decision 10). Group changes take effect at the person's next Windows sign-in.

### 14.2 The rights as NTFS entries

The folder script (deploy.md delivers it) sets these entries, written in `icacls` notation:

| Right | Entries on the folder |
|---|---|
| Read | `(OI)(CI)(RX)` |
| Modify | `(RX,WD,AD)` on the folder itself (list, create files, create folders), plus `(OI)(CI)(IO)(M)` for everything inside it. The folder itself can't be deleted or re-permissioned |
| Read and add files | `(RX,WD,AD)` on the log root (list, create files, create month folders); `(CI)(IO)(RX,WD)` for the month folders (list, create files); `(OI)(IO)(R)` for the files (read only) |
| On each log root, once | `*S-1-3-4:(OI)(CI)(IO)(R)` (OWNER RIGHTS). The creator of a log file or month folder doesn't get the owner's implicit right to change its permissions, so nobody can alter, rename or delete a log entry |
| Everywhere | `SYSTEM:(OI)(CI)(F)` and `BUILTIN\Administrators:(OI)(CI)(F)` (the server's). Inheritance from above `v2` is removed (`/inheritance:r`), and there is no CREATOR OWNER entry anywhere in `v2` |

The share's own (SMB) permissions must allow at least Change for Domain Users, so that the NTFS
entries decide. Creating and writing a log file through one handle needs only "create files" on the
folder: Windows gives the creator the access it asked for on the new file. The Windows test in §17.2
checks this.

### 14.3 What the folder script must do

The folder script is PowerShell and is delivered later. deploy.md owns how it is delivered, its
parameters and the README text. This is the contract it must meet:

1. Take the parameters `-ConfigPath` (today's config folder; v2 is its sibling), `-AdminGroup`
   (default "Stripes Administrators"), `-SenderGroup` (default "Stripes MapleJet Senders") and
   `-UsersGroup` (default "Domain Users"), plus `-WhatIf`. Run as an account with Full Control on the
   Stripes folder.
2. Create every folder in §1.1 that the script owns, if it is missing. Never delete or rename
   anything, and never touch today's `config\` or `status\` apart from reading the two Zebra files.
3. Set the entries of §14.2 on `v2` and its subfolders. Re-running gives the same result.
4. Copy `config\printers.json` and `config\stock.json` into `v2\config\` only if each is missing there
   (the one-time Zebra copy).
5. If `v2\config\hx\company.json` is missing, write the first one: the §4.1 fields with their
   `hx_model` defaults; `id` `"company"`; `revision` 1; `updated_by` = the account running the
   script; `updated_at` = now (epoch seconds); `admin_group`/`sender_group` = the group names; and
   `admin_group_sid`/`sender_group_sid` resolved with
   `([System.Security.Principal.NTAccount]"<name>").Translate([System.Security.Principal.SecurityIdentifier]).Value`.
   Write it as UTF-8 without BOM (`[System.IO.File]::WriteAllText` with `UTF8Encoding($false)`),
   including the envelope keys `format_version` 1 and `file_type` `company`.
6. If `v2\config\hx\changes.json` is missing, write
   `{"format_version": 1, "file_type": "changes", "counter": 1, "written_at": <now>, "written_by":
   "<account>", "last": null}`.
7. Print what it created and changed, then print a summary of the resulting rights for each folder.
8. Warn, without changing anything, if the share's offline caching isn't off (`Get-SmbShare`
   CachingMode, when the script runs on the file server) and remind IT that v2 must not be
   DFS-replicated (§8.2 note 5).

No new registry value is needed: Stripes finds v2 as the sibling of `ConfigPath`.

## 15. Interfaces added here

These are added to `hx_share`, on top of architecture.md §6.6. Nothing in that section changes.

```python
# constants
UPGRADES: dict[int, Callable[[str, dict], dict]] = {}   # format v -> v+1 upgrade of a decoded dict (file_type, d)
FILE_TYPES: dict[type, str]       # Company "company", Line "line", Machine "machine", Printer "printer",
                                  # MachineFilmAdjustment "adjustment", Calibration "calibration",
                                  # Product "product", Film "film", PrintText "print_text"
LOG_RESERVED_KEYS = ("format_version", "file_type", "log_kind", "logged_at", "logged_by",
                     "logged_on", "logged_pid", "log_name")
FILE_KINDS = ("front_photo", "back_photo", "artwork", "spec", "other")
RECORD_MAX_BYTES = STATUS_MAX_BYTES = 1_000_000
LOG_ENTRY_MAX_BYTES = 8_000_000
ATTACHMENT_MAX_BYTES = 50_000_000
FILM_PREVIEW_MAX_BYTES = 512_000
CHUNK_BYTES = 1_048_576
READ_RETRY_S = (0.05, 0.1, 0.2)
REPLACE_RETRY_S = (0.05, 0.1, 0.2, 0.4, 0.8, 1.6)
POOL_IDLE, POOL_MAX = 4, 16
CLOCK_BACK_TOLERANCE_S = 300.0
NETWORK_WINERRORS = frozenset({51, 53, 54, 59, 64, 67, 121, 1222, 1231, 1232, 1236, 2250})

class ShareDenied(ShareError): ...          # PermissionError on the share; the share stays available

# encoding (pure)
def encode_json(file_type: str, data: dict) -> bytes
def decode_json(raw: bytes, *, file_type: str | None, rel: str) -> dict
def record_rel(record: Record) -> str                        # e.g. "templates/films/fm_3f9a1c22b0/film.json"
def record_rel_for(cls: type, record_id: str) -> str
def make_log_record(kind: str, entry: dict, identity: Identity, at: float) -> dict   # §6.2; raises ValueError
def log_name(entry: dict, at: float) -> str                  # §6.3
def log_month(name: str) -> str                              # "2026-10"
def encode_log(stored: dict) -> bytes                        # §6.4; raises ValueError above LOG_ENTRY_MAX_BYTES
def decode_log(raw: bytes) -> dict | None                    # None: unfinished or damaged; FormatTooNew: newer
def safe_file_name(name: str) -> str                         # §2 attachment safe name
def score(printer_id: str, computer: str) -> int             # §12.4
def takeover_after_s(rank: int, timings: Timings = TIMINGS) -> float   # §12.5
def settle_s(timings: Timings = TIMINGS) -> float            # 0.4 * stale_s (§11.6)
def write_record(store: "ShareStore", record: Record) -> None
    # writes the record file as given: no revision bump, no permission check, no log, no
    # changes.json bump. For seeding test worlds (FakeWorld) only.
def self_check() -> None
    # in a tempfile.mkdtemp() folder: write_whole, read_bytes, create_exclusive (twice: True then
    # False), rename, encode/decode of JSON and of a log; raises on any failure

# FileOps / FlakyFileOps
class FileOps:
    tmp_tag: str = ""            # set by ShareStore (§2, §8.1)
class FlakyFileOps(FileOps):     # §16
    def __init__(self, *, clock: Clock = SYSTEM_CLOCK) -> None: ...
    down: bool; hang: bool; slow_s: float; hide_new_s: float; not_found_cache_s: float
    fail_replace: int; break_after_create: int
    calls: list[tuple[str, str]]
    def view(self) -> "FlakyFileOps": ...

# CounterWatch
class CounterWatch:
    def missing(self, key: str) -> float: ...
    def forget(self, key: str) -> None: ...

# ShareStore
class ShareStore:
    down_reason: Msg | None
    def probe_now(self) -> None: ...                          # non-blocking
    def read_bytes(self, rel: str) -> bytes | None: ...      # None if missing
    def write_bytes(self, rel: str, data: bytes) -> None: ...   # write_whole
    def create_once(self, rel: str, data: bytes) -> bool: ...   # create_exclusive
    def copy_in(self, rel: str, source_path: str) -> None: ...  # chunked whole-file copy (§13.5)
    def listdir(self, rel: str) -> list[str]: ...            # [] if the folder is missing
    def makedirs(self, rel: str) -> None: ...
    def remove(self, rel: str) -> None: ...
    def full_path(self, rel: str) -> str | None: ...
    def put_log_bytes(self, kind: str, name: str, raw: bytes) -> str: ...   # §6.5
    def list_logs(self, kind: str, *, since: float | None = None, entry_type: str = "",
                  ref: str = "") -> list[str]: ...
    def read_log(self, kind: str, name: str) -> dict | None: ...
    def poll_age(self, machine_id: str) -> float | None: ...
    def lock_age(self, printer_id: str) -> float | None: ...
    def check_poller_paused(self, machine_id: str, token: str) -> str: ...
        # "paused" | "stale" | "missing" | "polling" (§11.6)

# PrinterLock
class PrinterLock:
    @property
    def token(self) -> str: ...   # "" unless held
    def check(self) -> None: ...  # raises LockLost when state == "lost"

# ReachMemory (§12.3)
class ReachMemory:
    def __init__(self, local: LocalStore, *, clock: Clock = SYSTEM_CLOCK, timings: Timings = TIMINGS) -> None: ...
    last_printer_reach: float | None
    def reached(self) -> None: ...
    def can_poll(self) -> bool: ...
    def probe_due(self, *, unpolled: bool) -> bool: ...
    def probed(self, ok: bool) -> None: ...

# LocalStore
class LocalStore:
    def read_local_json(self, name: str) -> dict: ...         # <local>/hx/<name>; {} if missing/damaged/newer
    def write_local_json(self, name: str, data: dict) -> None: ...
    def has_preview(self, sha256: str) -> bool: ...
    def write_preview(self, sha256: str, png: bytes) -> None: ...
    def prune_previews(self, keep: set[str]) -> None: ...
    def queue_log(self, kind: str, name: str, stored: dict) -> None: ...
    def queued_logs(self) -> list[tuple[str, str, dict]]: ...    # (kind, name, stored), oldest first
    # queue_send_log(name, entry), pending_send_logs() and mark_uploaded(name) keep their §6.6 signatures;
    # "entry" there is the stored dict (envelope included)

# DataService
class DataService:
    problems: list[Msg]
    too_new: list[str]
    def run_once(self) -> None: ...                           # tests: one data cycle and one flush
    def append_log(self, kind: str, entry: dict) -> tuple[str, bool]: ...   # (name, queued)
    def save_preview(self, film_id: str, sha256: str, png: bytes) -> None: ...
    def preview_bytes(self, film_id: str, sha256: str) -> bytes | None: ...
    def share_path(self, rel: str) -> str | None: ...
```

## 16. Fault injection (FlakyFileOps)

`FlakyFileOps` is a real `FileOps` on real files with switchable faults. The switches can be changed
at any time from the test thread.

| Field | Default | Effect |
|---|---|---|
| `down` | False | Every operation raises `OSError(errno.EHOSTDOWN, "share down (fake)")` at once |
| `hang` | False | Every operation blocks in real time until `hang` is cleared or 30 s pass, then raises the same `OSError`. Used to test the timeout and abandoned threads |
| `slow_s` | 0.0 | Every operation sleeps this long in real time first |
| `hide_new_s` | 0.0 | `listdir` leaves out entries that another view created less than `hide_new_s` ago (`clock.monotonic()`). Like the SMB directory cache |
| `not_found_cache_s` | 0.0 | After `read_bytes`/`exists` of a path answered "not found", that view keeps answering "not found" for that path for this long, even if the file now exists. Like `FileNotFoundCacheLifetime` |
| `fail_replace` | 0 | The next N `os.replace` calls inside `write_whole` raise `PermissionError`, like a reader without share-delete |
| `break_after_create` | 0 | The next N `create_exclusive` calls create the file, write half the data, then raise `OSError(errno.EIO)` |
| `calls` | [] | `(operation, path)` for every call, in order, for assertions |

`view()` returns a FlakyFileOps that shares `down`, `hang`, `slow_s`, `fail_replace`,
`break_after_create` and the creation-time register with its parent, but has its own not-found cache
and its own `calls`. Each simulated PC gets its own view, so per-client caches are modelled. The
parent is a view too.

## 17. Tests

### 17.1 `test_hx_share.py` (every platform)

Logic tests use `FakeClock`. Threaded tests use `TIMINGS.scaled(0.01)` and real temp folders. None
sleeps more than 0.5 s in real time.

1. **Paths:** `v2_share_root("/srv/share/Stripes/config") == "/srv/share/Stripes/v2"`; a trailing
   separator gives the same result; `STRIPES_V2` overrides; None stays None. `roaming_dir`,
   `local_dir` and `machine_dir` follow their environment variables and the fallbacks in
   architecture.md §10.5. `copy_once` copies only when the new file is missing and the old one exists,
   and never overwrites.
2. **Envelope:** `encode_json` then `decode_json` round-trips. A BOM is accepted. A missing,
   non-integer or `True` `format_version` gives `ValueError`. Version 2 gives `FormatTooNew` with
   `found == 2` and `path == rel`. A wrong `file_type` gives `ValueError`. Unknown keys are ignored by
   `from_json`. `bytes` gives `TypeError` and NaN gives `ValueError`.
3. **write_whole:** replaces the target and leaves no `*.tmp`. With `fail_replace=3` it succeeds.
   With `fail_replace=99` it raises `PermissionError`, removes its tmp, and leaves the target
   unchanged.
4. **Records:** `DataService.save` of each record type writes exactly the §2 path with the envelope
   and revision 1. Saving again with `expected_revision=1` gives revision 2. `expected_revision=1`
   once more gives `RevisionConflict` with `current == 2`. A new record whose file exists gives
   `RevisionConflict`. A non-administrator gets `NotAllowed`. An unknown top-level key in the file
   survives a save. The admin log entry has `type == "record_saved"` and a printer token of
   `"(set)"`. The `changes.json` counter goes up by one per save. A calibration save copies
   `head_distance_mm` to the machine and bumps its revision. A FileRef path outside its film gives
   `ValueError`.
5. **Loading:** a seeded share loads completely. A damaged `film.json` adds a problem, keeps the
   previous `current`, and leaves the local copy unchanged. A format-2 film makes `can_send()` return
   `(False, "Update Stripes...")`. `desktop.ini`, `x.json.tmp` and a file whose id doesn't match its
   name are ignored or reported as specified. With `changes.json` unchanged, a cycle reads only
   `changes.json` (checked through `FlakyFileOps.calls`). With the counter changed, the cycle and the
   next one both reload in full. A missing `company.json` gives "MapleJet isn't set up on the server
   yet."
6. **Local copy and the 24-hour rule (`FakeClock`):** after a complete load the local copy holds only
   active films and print texts, and has their previews. `read_local_copy` gives
   `source == "local"`. A different `share_root` gives None. With the share down from t0:
   `can_send()` is True at t0 + 86,399 s and False at t0 + 86,401 s with "Can't reach the server
   since". A wall clock set back 299 s gives True; back 301 s gives False. Reaching the share again
   gives True.
7. **Timeouts:** `FlakyFileOps(hang=True)` with scaled timings: `read_json` raises
   `ShareUnavailable` within `share_timeout_s + 0.1` s; the next call raises in under 10 ms without
   a new entry in `calls`; after `share_retry_s` one probe runs; after clearing `hang`, the next probe
   makes `available` True. `down=True` gives `ShareUnavailable` at once. A `PermissionError` gives
   `ShareDenied` and `available` stays True. No more than `POOL_MAX` threads ever exist.
8. **Logs:** `append_log` writes `logs/<kind>/<YYYY-MM>/<name>`, and the name matches §6.3. Entries at
   1793491199.999 (31 Oct 2026 23:59:59.999 UTC) and 1793491200.0 land in `2026-10` and `2026-11`.
   `decode_log` of every proper prefix of a valid file gives None, and so does a file with one byte
   changed. `read_logs` skips an entry written with `break_after_create=1`. `list_logs` filters by
   type, ref and since without reading files (no `read_bytes` in `calls`). Reserved keys, a missing or
   bad `type`, and 8 MB of data each give `ValueError`. The example file in §6.4 decodes, and its
   sha256 matches.
9. **Queue:** with the share down, `append_send_log` returns `(name, True)` and `data.pending_logs() ==
   1`. When the share is back and a sender runs `run_once()`, the file is in `logs/hx-sends/<month>/`
   under the same name, a marker exists, and `data.pending_logs() == 0`. A non-sender leaves it queued.
   If an unfinished file of the same name is already on the share, the upload lands as `-2`. If the
   machine queue folder can't be written (a read-only folder, or a patched `makedirs` raising
   `PermissionError`), the entry goes to `<local>/hx/send-queue/`. `find_send` finds an entry on the
   share and one in the queue.
10. **CounterWatch (`FakeClock`):** a first sight gives 0. An unchanged counter gives the elapsed
    time. A change resets it. `missing` grows without a break and resets when the key is seen again.
    Keys untouched for an hour are pruned.
11. **Locks (`FakeClock`, two stores on one temp share):**
    - `acquire` creates the file with the envelope and a 16-hex token.
    - The second store gets `LockHeld` with the holder's user and computer.
    - With scaled threads, the keeper bumps the counter.
    - When the holder stops refreshing and the other store reads every 5 s: still `LockHeld` at
      59 s; at 60 s the takeover succeeds, the file has the new token, no `*.stale-*` is left, and
      `lock_taken_over` is in hx-events.
    - Two stores taking over the same stale lock at once: exactly one `"held"`.
    - The old holder after a takeover: `lost` at its next refresh, `check()` raises `LockLost`, and
      `release()` leaves the new file alone.
    - A store that first sees a stale lock gets `LockHeld`.
    - `release` removes the file and the `local_locks` entry, and is idempotent.
    - With the share down: `"local_only"`, and `local_locks` set.
    - A second acquire in the same process: `LockHeld`.
    - A damaged lock is held, then taken over after 60 s. A format-2 lock is held.
    - Refreshes failing for 30 s: `lost`, and no write after that.
    - `check_poller_paused` returns `"paused"` (token match, or own poller key), `"polling"` (fresh,
      other token), `"stale"` (15 s unchanged), and `"missing"` only after 15 s missing (at 14 s it
      is `"polling"`).
12. **Heartbeats:** `beat` writes the file and the counter goes up. A beating store is live. One
    stopped for 15 s isn't. Files with `format_version` 2 and damaged files are left out. The own
    heartbeat is included even with `hide_new_s=10`. A file unchanged for 600 s is removed.
    `remove()` deletes the file.
13. **Assignment:** the `score` test vector and the golden table of §12.4. Removing FLOOR02 changes
    exactly pr_…4, pr_…b and pr_…c. Two heartbeats from one computer are ordered by key. `can_poll`
    False leaves a heartbeat out. A printer with no pollers gets an empty list. The 1,000-printer,
    5-PC test gives each PC between 15 % and 25 %. `takeover_after_s` for ranks 0 to 4 gives 0, 15,
    20, 25 and 30, and scales with `Timings.scaled`.
14. **ReachMemory (`FakeClock`):** it survives a new instance. `can_poll` is True up to 86,400 s and
    False after. `probe_due` follows the §12.3 table for both `unpolled` values.
15. **Status files:** `write_poll`/`read_poll` round-trip at the §2 path, and `poller` is set by the
    store. A counter that doesn't go up gives `ValueError`. `write_expected` sets `counter` to the
    file's + 1, plus `written_by` and `computer`. A format-2 `poll.json` raises `FormatTooNew`, and
    `poll_age` still tracks its counter. `write_ack` twice: the first ack stays. `read_ack` caches it
    (one `read_bytes` for two calls). `preview.bmp` round-trips.
16. **Not-found cache:** with `not_found_cache_s=5` on store B's view, a lock created by store A is
    invisible to B's `read_lock` for 5 s and visible after. `settle_s()` with the default timings is
    6.0, which is more than 5.

### 17.2 `test_hx_share_windows.py` (`skipUnless(sys.platform == "win32")`)

1. **Swap while open:** a thread holds the target open with `hx_win.open_for_read`.
   - Held for 0.3 s: `write_whole` succeeds (with retries if needed).
   - Held for 5 s: `write_whole` either succeeds or raises `PermissionError` after its retries, and
     the target's bytes are always exactly the old or exactly the new content. The test records
     which happened (printed) and passes either way.
2. **Readers never break a writer:** one thread runs `write_whole` 200 times while another reads in
   a loop. Every read parses as either the old or the new JSON, and every write succeeds.
3. **open_for_read shares delete:** while one is open, another handle can rename and delete the file.
4. **create_exclusive race:** two threads, 100 rounds. Each round, exactly one gets True.
5. **Add-only folder:** a temp folder with `icacls <dir> /inheritance:r /grant:r "%USERNAME%:(RX,WD,AD)"
   "%USERNAME%:(OI)(IO)(R)" "*S-1-3-4:(OI)(IO)(R)"`.
   - `create_exclusive` with data succeeds, and the file reads back complete.
   - Opening it for writing, `os.replace` onto it, `os.remove` and `FileOps.rename` all fail with
     `PermissionError`.
   - Clean-up grants Full control first.
   - The test is skipped if `icacls` fails.

## 18. Notes for the other spec files

**states.md (Poller, StatusWatcher)**

- Write `poll.json` every cycle with the counter + 1, and the picture before the `poll.json` that
  names it.
- Follow §11.6 and §12.6 exactly: the lock before every request, `paused_for_lock` written at once,
  the settle rule on every start, a missing file counted only after `stale_s`, handover through
  `yielded_to`, and stopping share-mode polling after `stale_s / 2` of failed share writes.
- Use `ReachMemory` and the probe rules of §12.3.
- Log through `DataService.append_log("hx-events", ...)`.
- Use `poll_age`/`lock_age` for views.
- Treat `FormatTooNew` from `read_poll` as a fresh, unreadable poll file ("Update Stripes" on that
  machine), never as missing.
- Keep poller memory in `poller-<name>.json` through `read_local_json`/`write_local_json`.

**send.md**

- Hold the lock with `with store.lock(...)`, and call `lock.check()` before each request.
- Implement STEP_WAIT_POLLER with `check_poller_paused` as in §11.6, re-checking right before the
  first request, with a time limit.
- Test prints and calibration should require `state == "held"` and refuse `local_only`: a test runs
  for minutes, and the plan wants the lock held throughout.
- The main log entry of a Send has `type == "send"` (for `find_send`). Store pictures as base64 under
  `*_b64` keys.
- `FormatTooNew` from `read_expected` means: don't send to that machine ("Update Stripes").
- Write logs through `DataService.append_log`/`append_send_log`.

**ui.md**

- After `save_file` of an image, make a PNG preview (longest side at most 480 px, 512,000 bytes at
  most) and call `save_preview`.
- Show `DataService.problems`.
- Retry is `store.probe_now()` plus `data.reload()`.
- Open originals with `share_path` on a worker thread.
- `HxSettings.load`/`save` use `read_user_json("maplejet.json")`/`write_user_json`.

**protocol.md (hx_fake)**

- `FakeWorld.store(identity)` passes `self.fs.view()`, so each simulated PC has its own caches.
- `FakeWorld` seeds records with `hx_share.write_record` and writes a `changes.json`.
- `FakePrinter.max_in_flight` counts HTTP requests. A connection that closes without sending a byte
  (the reachability probe) isn't one.

**deploy.md**

- The folder script follows §14.3.
- The exe's self-check may call `hx_share.self_check()`.
- The README describes the v2 folders, the groups and the IT checks in §8.2 note 5.

## 19. OPEN items

| Item | What to confirm | Safe default built now |
|---|---|---|
| SMB not-found cache | Whether an open by name on the real share hides a new lock file, and for how long (`FileNotFoundCacheLifetime`, 5 s by default) | Handshake, plus a missing poll file counted only after `stale_s`, plus a 6 s settle before a starting poller's first request (§11.6). If the measured delay is longer, raise `settle_s` above it |
| Replace while another PC reads | Whether the file server lets `os.replace` through while another client holds the target open with FILE_SHARE_DELETE | Handles held for milliseconds only; about 3.2 s of replace retries; a failed status write is simply retried next cycle |
| Reachability probe | A bare TCP connect to port 80 doesn't disturb a printing printer | At most hourly when the PC has reach, every 60 s only while some printer has no poller; never to a locked printer; skipped if this PC's gate is busy |
| `%PROGRAMDATA%` permissions on the floor-PC image | Every user can create files in `%PROGRAMDATA%\Stripes\v2\send-queue` | Falls back to the user's own `<local>/hx/send-queue/` |
| Add-only ACLs on the file server | The §14.2 entries (OWNER RIGHTS, create-only) behave on the server's Windows version as in the §17.2 test | The completion marker and read-only readers keep logs safe even if a writer could append |
| Share settings | Offline caching off; v2 on a single server, not DFS-replicated | The folder script warns; README tells IT |
| Log volume | Month folders stay quick to list (estimated at most about 20,000 files a month) | Month folders; `list_logs` filters by name before reading |
| Bag counter, print speed units, encoder calibration, film print length | (architecture.md §11) | Stored as `Company.bag_counter` "PMCNT", `Machine.print_speed` null, `Calibration` record and `Machine.head_distance_mm` null, `Film.print_length_mm` null. Null blocks sending where architecture.md says so |
| Beacon | None (`BEACON_SUPPORTED = False`) | Nothing is stored for it |

## 20. Decisions made in this spec

1. **Envelope keys** are `format_version` and `file_type`, in every hx JSON file, with the dataclass
   fields flat beside them. `file_type` catches a file in the wrong place.
2. **Stable keys** (§3.4) survive every future format, so an older PC still sees a newer PC's live
   poll status and locks as live.
3. **A newer record blocks all sending on that PC** ("Update Stripes"), rather than per record. It's
   the simplest safe rule, and a format change is a rare, planned event.
4. **A damaged or newer record makes the load incomplete**, and the last complete set stays in use.
   It is reported, never silently skipped.
5. **`changes.json`** lets every PC skip the full reload of about 1,000 files every minute, with a
   forced full reload every `10 × data_refresh_s` and a second reload after each change (directory
   cache).
6. **Logs in UTC month folders**, named `<stamp>.<type>.<ref>.<rand>.json`, so listings stay small
   and filters work on names without opening files.
7. **The completion marker is a second line, `{"end": sha256}`.** A truncated, unfinished or altered
   file never passes, and each line is still plain JSON.
8. **Logs never go through `ShareStore.append_log` directly in app code.** `DataService.append_log`
   queues any kind (sends, events, admin) when the share can't be written, so no entry is lost.
9. **One PC queue for all three log kinds**, with uploads gated by group, and a per-user fallback
   folder when `%PROGRAMDATA%` can't be written.
10. **Upload duplicates:** identical bytes count as already uploaded. An unfinished file with the
    same name means the upload goes under `-2`…`-9`.
11. **A missing `poll.json` counts as stale only after `stale_s`**, and **a starting poller waits a
    6 s settle**. Both guard against the SMB not-found cache. They narrow architecture.md §7.3
    without changing its rules.
12. **A sender re-checks the poll file right before its first request** after seeing "stale" or
    "missing".
13. **`check_poller_paused` treats this PC's own poller as paused**: the in-process `local_locks`
    already stops it.
14. **A lock holder stops refreshing after 30 s of failed refreshes** (half of `lock_stale_s`). It
    can never recreate its file after another PC's takeover.
15. **A takeover that moved the wrong file aside puts it back** and raises `LockHeld`.
16. **Released locks whose removal failed** are retried for 10 minutes and ignored by this PC's own
    poller.
17. **A damaged lock is still a lock**, judged by the crc32 of its bytes, and taken over after 60 s.
18. **Rendezvous score by computer, not process**, with ties by key. A restart keeps a PC's
    printers; two users on one PC back each other up; the load spreads across PCs.
19. **Reachability probe frequency:** hourly while the PC has reach, every 60 s only while a printer
    has no poller. The connect is never made to a locked printer, and is skipped if this PC's own
    gate for that printer is busy. A printer with no live poller is preferred.
20. **The 24-hour rule uses this PC's wall clock**, so it survives restarts, with 300 s of tolerance
    for a clock set back. `reached_at` moves on every successful change check, not only on full
    reloads.
21. **The local copy is one JSON file** with only active films and print texts, plus content-addressed
    PNG previews. It is tied to the share root it came from.
22. **Film photo previews are made once, by the admin's PC**, and stored on the share. Other PCs only
    copy small PNGs, so no PC below the UI layer needs Pillow.
23. **Film originals are named by a sha256 prefix plus a safe name**, copied in 1 MiB chunks, so each
    chunk respects the 5 s share timeout. 50 MB at most.
24. **Admin saves check the revision, write, then read back.** Two administrators saving the same
    record in the same instant both end up in the admin log, and the second gets `RevisionConflict`.
    Unknown keys are kept on save.
25. **Printer tokens never appear in logs** (`"(set)"`).
26. **Acks are created exclusively** (the first one wins), never deleted, and cached once seen.
    Alerts stay inside the poller's `poll.json`, which has one writer.
27. **`write_expected` and `write_line_status` set the counter and the writer fields themselves**
    (file + 1). `write_poll` sets `poller` and `written_at` and refuses a counter that doesn't go up.
28. **Temp-file tag** = sha256 of computer and user (12 hex) plus the pid. It keeps paths short, and
    lets a restart clear its own crash leftovers.
29. **Share error mapping:** a set list of network errors, or a timeout, marks the share down.
    A permission error is `ShareDenied` (a new `ShareError` subclass), and the share stays up.
30. **The probe runs lazily** on the next call after `share_retry_s`, with no extra thread.
    `probe_now()` serves the Retry button.
31. **Heartbeat files unchanged for 600 s are removed** by any PC that sees them.
32. **ACL details for the folder script:** no CREATOR OWNER anywhere, OWNER RIGHTS read-only on the
    log roots, and "Modify" that can't delete or re-permission the folder itself.
33. **No installer:** `Stripes.exe` stays a single file, deployed as today, and creates every PC
    folder itself on first use. Nothing on the PC needs administrator rights.
