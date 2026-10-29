# MapleJet Hx build spec

The build spec for adding MapleJet Hx thermal-inkjet bag printers to Stripes. Parallel coding agents
implement it with no other context, so every file is precise about names, types, files, limits,
timings, errors and tests. Nothing in it has been built yet.

The output stays exactly what it is today: one single-file `Stripes.exe` built by PyInstaller, signed
in CI and deployed by Group Policy as before. There is no MSI, installer, `--quit` option or old-copy
detection (owner decision, 29 Oct 2026).

## The files

Authority, highest first: the agreed plan (product behaviour), then architecture.md (names, types,
boundaries, threads), then each file for the modules it owns. Where two files disagree, the one with
more authority wins, and the other is a bug in the spec.

| File | Part | What it covers | Modules and files it owns |
|---|---|---|---|
| [architecture.md](architecture.md) | 1 | Module map and layers, every public name and shared dataclass (`hx_model`), threads, the error model, the test set-up, the `stripes.py` and `i18n.py` changes, paths, plan deviations (§12.1) | `hx_core`, `hx_model`, `hx_win`, the `stripes.py` and `i18n.py` changes |
| [protocol.md](protocol.md) | 2 | Autopilot and the web read-back: HTTP on raw sockets, token and scrambling, the query catalogue and reply checks, the write whitelist, retries, timeouts and worst cases, the per-printer gate, the SME parser; the fake printer, fake time server and fake world; the fixtures | `hx_protocol`, `hx_fake`, `testdata/hx/` |
| [storage.md](storage.md) | 3 | The v2 folders, every JSON file format, whole-file writes and swap-safe reads, the share timeout, printer locks, heartbeats and poller assignment, the local copy and the 24-hour rule, logs and the upload queue, permissions, fault injection | `hx_share` |
| [states.md](states.md) | 4 | The machine state codes, judging and the two-reading rule, crash recovery, alerts and acknowledgements, views and the floor-board rows, the poller (cycle, budget, backoff, daily checks, the daylight-saving clock set, standalone mode), the status watcher | `hx_state`, `hx_poller` |
| [send.md](send.md) | 5 | Origin wording, the text builder, validation and plan merge, print position and delay, message names, compare and fingerprint, time and SNTP, the Send pipeline, confirmation, failures, Stop, Resume, Set clocks, expected-state writes, test print and calibration, logs | `hx_message`, `hx_time`, `hx_send` |
| [ui.md](ui.md) | 6 | The two top tabs and every MapleJet screen: Send, result, red window, Stop/Resume/Set clocks, cards, monitor, floor board, admin screens, film editor, print-text designer, test print and calibration windows; services and start-up; every UI text with its Spanish | `hx_ui`, `hx_ui_common`, `hx_ui_send`, `hx_ui_monitor`, `hx_ui_admin`, `assets/alert.wav` |
| [deploy.md](deploy.md) | 7 | `pyinstaller.spec`, `make_version.py` and the printer-password key (`hx_key.py`), CI, the self-check, README updates; specs for the v2 folder script and the test-PC instructions, delivered after development | `pyinstaller.spec`, `make_version.py`, `test_make_version.py`, `test_deploy.py`, `.github/workflows/build.yml`, `build.bat`, `.gitignore`, the root `README.md`, the generated `hx_key.py`; later `tools/New-StripesV2Folders.ps1`, `test_folder_script.py`, `docs/maplejet/test-pc.md` |

Each file ends with its own OPEN items and "Decisions made in this spec".

## Public repository rule

This repository is public. Nothing in it (docs, code, tests, fixtures, comments) may contain a real
IP address, host name, printer UID or printer name, a password or password hint, a token, a customer
name or the company's domain. Examples use only:

- addresses 192.0.2.x (TEST-NET-1), printer UID 1109 with password 123321 (the manual's own example);
- `time.example.local`, "Example Co", "Example Co 2lb Onion", "Line 1", "Line 1 Bagger 1";
- users `EXAMPLE\jsmith` and `EXAMPLE\akim`, PCs `FLOOR01` and `FLOOR02`.

The printer-password key is a CI secret and never appears in the repository, a log or an artifact
name (deploy.md §3).

## Running the tests

The suite uses `unittest`, as the existing test files do, and needs Python 3.12 with Tk plus the
packages in `requirements.txt` (Pillow, segno; pywin32 and pystray on Windows). From the repository
root:

- Linux: `xvfb-run -a python -m unittest -v` (the Tk tests need a display; they skip themselves when
  Tk can't start).
- Windows: `python -m unittest -v`. CI runs exactly this on `windows-latest` before every build.

The whole suite should stay under about two minutes: no test sleeps more than 0.5 s of real time,
threaded tests use `TIMINGS.scaled(...)` and logic tests use `FakeClock` (architecture §9). Windows-only
share tests (`test_hx_share_windows.py`) skip elsewhere.

## Owner sign-off needed

These are built as described, the safe way, but differ from the plan's wording or accept a risk:

1. The font list and firmware version are read with two small web GETs (`/sfl.xml`, `/idn.xml`),
   besides the message Load the plan calls "the only web request" (architecture §6.4, §12.1).
2. When only one PC loses the share, its standalone checks may overlap the other PCs' polling and
   their Sends get no `no_lock` warning; standalone checks start only after 60 s of continuous outage
   (architecture §7.3 rule 7, states.md §6.12).
3. A Send made while the share is down is recorded when the share returns only if the sending PC
   hasn't restarted meanwhile; otherwise its machines show red drift until the next Send (send.md
   §14.1).
4. Every published release contains the printer-password key, and releases on this public
   repository can be downloaded by anyone (deploy.md §3.7, OPEN 1).

## OPEN items

Each has a safe default that is built now; confirm on the test unit (or with IT) and change only what
the item names.

### The printer

| Item | Safe default built now | Where |
|---|---|---|
| Encoder calibration method (spacing check, head-distance print, the 2 mm tolerance) | the procedure of send.md §16 and ui.md §13; an encoder machine without a head distance can't be sent to; Stripes never changes the wheel size | architecture §11, send.md §16, ui.md §13 |
| Which counter counts bags | `Company.bag_counter = "PMCNT"`; `"none"` makes test steps timed | architecture §11, states.md §14 item 1 |
| Print speed units and range | cm/min, 1 to 2500, no default (empty blocks sending); whether 2400 is accepted | architecture §11, protocol.md §19, send.md §21 |
| Beacon | never queried or switched (`BEACON_SUPPORTED = False`) | architecture §11 |
| Font list and firmware source | `GET /sfl.xml` (FON1..FON6) and `GET /idn.xml`; whether they need the cookies and don't disturb printing | protocol.md §10.8, §19 |
| Film print length (film-stop warning) | no warning without `Film.print_length_mm`; "Print length unknown" shown | send.md §5.4 |
| Item positions, gaps and the print area | first item at 0,0, others relative; gaps 0 to 50 canvas units; an item whose BOUND leaves 0..1024 by 0..160 fails the read-back (lower the x limit to 256 if the canvas clips) | send.md §8.1 rule 6, §21 |
| `&`, `<`, `>` and `ñ` | `<` and `>` refused; `&` escaped when sent, compared raw | send.md §2.1, protocol.md §19 |
| TD ranges, the midnight rollover, seconds | HOUR 0 to 23, rollover (0, 0), rounded to the minute, 90 s read-back tolerance | send.md §9.4, protocol.md §19 |
| Replies to PRINT, PRINTMSG, TD and CPPARM | a first token of `0` means done | protocol.md §6.3 |
| DELAY in the read-back | µs, compared exactly | protocol.md §5.3 |
| RESNAM spellings, RESO | UltraFine, Fine, Normal, Draft; RESO ignored | send.md §21 |
| Web login and sessions | both cookies required; whether two PCs keep separate sessions; the session lifetime | protocol.md §10 |
| PRINTMSG to an empty slot; MSCNT on a message change | the fake answers 4 and resets MSCNT | protocol.md §19 |
| A Load of slot 0 while printing is off | its slot is taken as the selected message | send.md §21 |
| OBJID numbering, item defaults, a relative item's POS | the defaults seen; POS not compared when not sent | protocol.md §19 |
| The printer echoes any TN | Stripes TNs are 42 mod 97 | protocol.md §10.4 |
| A bare TCP connect (the reachability probe) | allowed, only to a printer nobody polls | storage.md §12.3 |
| BPIX | taken as the number of black pixels; only "more than 0" is checked | protocol.md §19 |
| INKL meanings; PRIN after an error | 0 cartridge error, 2 and 3 low, 4 out of ink; both alerts may fire | states.md §14 |
| Whether the ink name follows a cartridge swap | treated as a settings check | send.md §10.5 |
| Power cycle: which message loads, whether printing comes back on | printing off gives amber `not_printing`; the first Load is checked for drift | states.md §14 item 2 |
| Best-by month arithmetic at month ends and on 29 Feb | the day cut to the month's end; any difference fails the date check | send.md §3.2 |
| Empty messages at the end of a test | CRTMSG with no name, SAVEMSG with no items | send.md §15.5 |
| Font names on the test print | `hx_model.STANDARD_FONTS` | send.md §15.3 |
| Busy codes on web replies; a timed-out request still running | classified busy; the gate stays held `read_retry_s` after `NoReply` | protocol.md §19 |

### The site and IT

| Item | Safe default built now | Where |
|---|---|---|
| SMB not-found cache on the real share | handshake, a missing poll file counted only after 15 s, a 6 s settle before a starting poller's first request | storage.md §19 |
| `os.replace` while another PC reads the file | handles held for milliseconds; about 3.2 s of retries | storage.md §19 |
| `%PROGRAMDATA%` permissions on the floor-PC image | falls back to a per-user queue | storage.md §19 |
| The add-only ACLs on the file server's Windows version | as the Windows-only test expects | storage.md §19 |
| Offline caching off; v2 on one server, not DFS-replicated | the folder script warns; README tells IT | storage.md §19, deploy.md §11 |
| Log volume per month folder | month folders, filtered by name before reading | storage.md §19 |
| PowerShell execution policy on the file server | the folder script is delivered unsigned | deploy.md §11 |
| Alert sound heard on floor PCs; loudness | the self-check checks the file and the API; amplitude 0.45 | deploy.md §11, ui.md §18 |
| A firewall prompt for SNTP or printer HTTP | outbound only, loopback only for the self-check | deploy.md §11 |
| Floor-board readability; full screen with two screens | rows fill the screen, at most a quarter each | ui.md §18, states.md §14 item 9 |
| Shift-end switch-offs inside 5 minutes raising `went_silent` | acknowledging turns it into grey Off; lower `silent_window_s` if noisy | states.md §14 item 3 |
