# MapleJet Hx build spec: states

Part 4 of 7. This file specifies `hx_state` (the per-machine state machine, alerts, line aggregation,
views and floor-board rows; pure) and `hx_poller` (the `Poller` and the `StatusWatcher`). Names, types,
layers and threads come from architecture.md and are not changed. storage.md's file rules and
protocol.md's request rules are followed as written. New public names are added only inside `hx_state`
and `hx_poller`, plus optional fields on `Reading`, `Judgement`, `PollStatus` and `Alert`. §11 lists all
of them.

Contents: 0 Scope and conventions · 1 The model · 2 State codes · 3 Judging a machine · 4 Alerts ·
5 Views · 6 The poller · 7 The status watcher · 8 Floor board and monitor cards · 9 Event log entries ·
10 Texts and Spanish · 11 Interfaces added here · 12 Tests · 13 Notes for the other spec files ·
14 OPEN items · 15 Decisions made in this spec

## 0. Scope and conventions

1. Authority: the plan, then architecture.md, then this file for behaviour inside `hx_state` and
   `hx_poller`. storage.md owns the files, protocol.md owns printer requests and reply checks, send.md
   owns what senders and testers write in `ExpectedState` and `LineStatus`, and ui.md owns the widgets.
2. Short names used throughout:

   | Name | Meaning |
   |---|---|
   | E | the machine's `ExpectedState` (`status/machines/<id>.expected.json`), or None when missing or damaged |
   | P | the machine's `PollStatus` (`status/machines/<id>.poll.json`) |
   | L | the printer's `LockInfo` (`status/locks/<printer_id>.lock`), or None |
   | fresh lock | a lock whose `lock_age` is below `lock_stale_s` (60 s) on this PC's clock |
   | stale lock | a lock whose `lock_age` is at or above `lock_stale_s` |
   | R | the running message: a `RunningMessage` from a web Load of slot 0 |
   | trusted | the printer answered and is the right printer: `reachable` and `problem == ""` (§3.7) |
   | the sender | the person who started the Send in question: `E.sender` (send.md §1.4), or `L.user` of its lock |
   | last sender | `E.sender`: who made the Send or test that set what this machine should print. Never `E.written_by`, which is only the last writer (a confirm, Leave it or clock set by someone else rewrites E without changing `sender`). When `E.sender` is `""` (no Send yet), there is no last sender |
   | notify user | `Line.notify_user` |

3. Colours are `hx_model.GREEN`, `AMBER`, `RED`, `GREY` and `BLUE`.
4. Time. The poller measures every duration on its own `clock.monotonic()` and hands `judge()` the
   results (`bags_recent`, `foreign_client`, the failure count), so `hx_state` stays pure. Wall times
   (`clock.time()`) are for display, logs, `MachineSnapshot.since` and `Alert.started_at` only.
   Freshness of other PCs' files is judged only from counters (`ShareStore.poll_age`, `lock_age`).
5. Examples follow the public-repo rule and storage.md's example IDs: machine `mc_5d1e2f3a4b`, printer
   `pr_9c8b7a6d5e`, line `ln_1a2b3c4d5e`, Send `sd_8a7b6c5d4e`, users `EXAMPLE\jsmith` and
   `EXAMPLE\akim`, computers `FLOOR01` and `FLOOR02`, "Line 1 Bagger 1".
6. Packaging doesn't change anything here. The output stays the single-file `Stripes.exe`, deployed as
   today, with no installer.

## 1. The model

Each machine has three layers. Each layer has one writer.

| Layer | Where | Writer | What it says |
|---|---|---|---|
| Expected | E | the printer-lock holder: Send, test print, calibration, Stop, Resume, Set clocks, confirm, Leave it (send.md) | what should be on the printer, whether it should be printing, and whether the sender confirmed it |
| Observed and judged | P | the assigned poller (§6) | the committed printer facts, the state code judged from them against E and L, and the machine's active alerts |
| Shown | `BoardView` in memory | every PC's `StatusWatcher` (§7) | P's judgement, overlaid with what needs no printer reading: a fresh lock (blue), staleness (NOT MONITORED), an E the poller hasn't judged yet, and acknowledgements |

Rules:

1. Only the poller judges printer readings, and only through `hx_state.judge()`. Only
   `hx_state.machine_view()` and `board_view()` overlay. The UI reads `MachineView`, `LineView` and
   `FloorRow` as they are and never decides a code, colour, button or sound (architecture §5.10).
2. A state code or an alert changes on a printer reading only when a second reading about a second
   later agrees (§3.8). Changes that come only from E, L or acknowledgements don't need one.
3. Alerts are part of P, so they have one writer. Acknowledgements are separate files, written once
   (storage.md §5.6).
4. The poller judges "printing off" and drift against E (plan). When E doesn't expect printing, a
   printer that is off is fine; when it does, it isn't.

## 2. State codes

`hx_state.STATE_CODES` maps every code to its colour (the "Colour" column). The headline is
`Msg(STATE_TEXTS[code], values)`; §10 has the Spanish. "Set by": **view** codes are produced only by
`machine_view`/`board_view` and are never written to P; **judge** codes are produced by `judge()` and
written in `P.state`; **poller** codes are written by the poller outside `judge()`.

"Allowed" lists what the state allows (§5.3 has the exact rules). The UI also requires the sender group
for Stop, Resume, Send and Set clocks, and the same user for Confirm. Acknowledge depends only on
alerts (§4.5), never on the code.

### 2.1 View codes

| Code | Colour | Headline (English) | Entered when | Left when | Allowed |
|---|---|---|---|---|---|
| `send_in_progress` | BLUE | Sending: {user} on {computer} | a fresh lock with purpose `send`, and E.mode isn't `switch_pending` | the lock is released (P's code shows) or goes stale (§3.6) | none |
| `switch_pending` | BLUE | Switching to the new message: {user} on {computer} | a fresh `send` lock and E.mode `switch_pending` | as above | none |
| `test_running` | BLUE | TEST by {user}, step {step} of {of} (or TEST by {user} before the first step) | a fresh lock with purpose `test` or `calibration` | released (normally then `test_finished`), or stale (§3.6) | none |
| `action_in_progress` | BLUE | Stopping printing: {user} · Resuming printing: {user} · Setting the clock: {user} (by purpose `stop`, `resume`, `clock`) · Printer in use: {user} on {computer} (any other purpose, such as a damaged lock's `unknown` or a newer version's purpose) | a fresh lock with one of those purposes, or any purpose not listed in this table | released or stale | none |
| `checking` | GREY | Checking... | no P has been read yet, and it hasn't been missing for `not_monitored_s`; or P's age isn't known (§7, a P first seen long after it was written); or P holds it (also a poller code: a worker starting after a long gap writes it until its first judgement, §6.3 step 1). The origin shows as unknown and `printing` is None | P is read, or 30 s pass (then `not_monitored`); the worker's first judgement | none |
| `not_monitored` | GREY, or the last known RED or AMBER (§5.2) | NOT MONITORED | P's counter unchanged, or P missing, for `not_monitored_s` (30 s) on this PC's clock. Standalone: §6.12 | P's counter moves | Confirm |
| `update_stripes` | GREY | Update Stripes to see this machine | P or E is from a newer format | this PC runs the newer Stripes | none |
| `not_set_up` | GREY | No printer set up | (board_view) the machine's printer record is missing, inactive, or has no password (`token == ""`) | an administrator fixes it | none |

### 2.2 Judge and poller codes

| Code | Colour | Headline (English) | Entered when (§3.4 has the exact conditions) | Left when | Allowed |
|---|---|---|---|---|---|
| `internal_error` | RED | Stripes hit a problem checking this machine | (poller) an exception that isn't an `HxError` in the worker cycle (architecture §8.1) | the next cycle that judges | none |
| `wrong_printer` | RED | Wrong printer at this address | `read("HXUI")` answered another UID (§6.5), confirmed by the second reading | the UID is right again | none |
| `login_failed` | RED | Stripes can't log in to this printer | `AuthFailed` | a reply that isn't `AuthFailed` | none |
| `test_orphaned` | RED | Test message still printing | a stale `test`/`calibration` lock, or E.mode `test_running` without a fresh lock, and printing is on | printing read off (then `test_interrupted`), or a new lock | Stop, Send |
| `test_interrupted` | RED | Test stopped on {computer}: needs a Send | as above, printing not on | a Send | Send |
| `interrupted_unknown` | RED | Send interrupted on {computer}: check the bags now | §3.6 | §3.6 | Stop, Send |
| `interrupted_old` | RED | Send interrupted on {computer} | §3.6 | §3.6 | Stop, Send |
| `interrupted_new` | AMBER | Send interrupted, not confirmed | §3.6 | §3.6 | Stop, Send, Confirm (only when E.mode is `running`) |
| `stop_failed` | RED | Still printing: stop it at the printer now | E.mode `stop_requested_at_printer` and printing read on | printing read off, or E changes | Stop |
| `stop_requested` | RED | Stop it at the printer: press Print On/Off | E.mode `stop_requested_at_printer` and printing not read since, or the printer isn't answering | printing read on (`stop_failed`) or off (`stopped_needs_send` or `stopped`) | none |
| `failed_after_switch` | RED | Send failed after the switch: check the bags now | E.failure with `after_switch`, no Leave it | Leave it (`left_red`), or a Send | Stop, Send |
| `failed_before_switch` | RED | Send failed: still printing the previous message | E.failure without `after_switch`, no Leave it | Leave it, or a Send | Stop, Send |
| `left_red` | RED | Left red by {user} | E.failure and E.left_by | a Send | Stop, Send |
| `rejected` | RED | Rejected by {user} | E.rejected and E.mode `running` (the sender said "Something's wrong" and hasn't stopped it) | Stop (`stopped_needs_send`), or a Send | Stop, Send |
| `printing_on_unexpected` | RED | Printing was switched on at the printer | printing read on while E.mode is `stopped_by_user` or `test_finished` | printing read off, or E changes | Stop, Send |
| `unreachable_printing` | RED | Not answering while printing bags | the printer stopped answering after printing bags in the last `silent_window_s` (§3.7) | it answers again, or the alert is acknowledged (then `off`) | none |
| `printing_off` | RED | Printing was switched off at the printer | E.mode `running`, printing read off after it was read on | printing read on, a Stop or a Send | Resume |
| `drift` | RED | Not printing what Stripes sent | E.mode `running` and R isn't the expected message (§3.3) | R matches again, or a Send | Stop, Send |
| `unknown_while_down` | AMBER | Can't check the message while the server is down | standalone mode only (§6.12) | the share is back | Stop |
| `ink_out` | RED | Out of ink | INKL 4 while printing | INKL isn't 4, or printing is off | Stop |
| `cartridge_error` | RED | Cartridge not detected | INKL 0 while printing | INKL isn't 0, or printing is off | Stop |
| `stopped_needs_send` | RED | Stopped, needs a Send | E.rejected and stopped (E.mode `stopped_by_user`, or `stop_requested_at_printer` with printing read off) | a Send | Send |
| `test_finished` | RED | Stopped, needs a Send | E.mode `test_finished`, printing not on; or E.stop_reason `test` (a test whose end procedure couldn't confirm printing off, then stopped later) with E.mode `stopped_by_user`, or `stop_requested_at_printer` and printing read off | a Send | Send |
| `off` | GREY | Printer off | the printer isn't answering and the machine isn't `unreachable_printing` | it answers | none |
| `not_printing` | AMBER | Printing is off | E.mode `running`, printing read off, but printing wasn't read on since the printer last came back (it was off or restarted) | printing read on, a Stop or a Send | Resume |
| `message_unchecked` | AMBER | Can't read the message on this printer | E.mode `running` (or `switch_pending`), trusted, and R isn't known: no successful Load since the last lock or E change, or the poller's message check has failed for long (C11b) | a Load succeeds | Stop, Send (Confirm while unconfirmed) |
| `unconfirmed` | AMBER | Send not confirmed | E.mode `running`, not confirmed, printing on or not yet read, R known | the sender confirms, or says something's wrong (`rejected`) | Stop, Confirm |
| `confirmed_with_notes` | AMBER | Printing, not fully checked | E.mode `running`, confirmed, E.notes not empty (`pc_time`, `no_lock`, `local_copy`), R known | a Send without notes | Stop |
| `not_from_stripes` | AMBER | Not sent from Stripes yet | no E (or mode `none`), printing on (with the no-sound warning `not_from_stripes`, §4.1) | the first Send | Stop, Send |
| `stopped` | GREY | Stopped by {user} | E.mode `stopped_by_user` (or `stop_requested_at_printer` with printing read off), not rejected, `E.stop_reason` isn't `test` | Resume or a Send | Resume, Send |
| `idle` | GREY | Not printing | no E (or mode `none`), printing off or not read | the first Send | Send |
| `running_confirmed` | GREEN | Printing | E.mode `running`, confirmed, no notes, printing on or not yet read, and R known and matching (a successful Load since E last changed) | | Stop |

The assignment's names map to codes as follows: Off `off`; Running confirmed `running_confirmed`; Send
in progress `send_in_progress`; Switch pending `switch_pending` (lock fresh) or the `interrupted_*` codes
(lock stale); Unconfirmed `unconfirmed`; Failed before/after switch `failed_before_switch`,
`failed_after_switch`; Left red `left_red`; Stopped needs Send `stopped_needs_send`; Stop requested at
printer `stop_requested`; Stop failed `stop_failed`; Test running `test_running`; Test finished
`test_finished`; Drift `drift`; Not monitored `not_monitored`; Unreachable while printing
`unreachable_printing`.

## 3. Judging a machine

`judge(prev, reading, expected, lock, machine, line, data, timings) -> Judgement` (architecture §6.2) is
the poller's only decision function. It calls `classify()` (§3.4) twice at most and applies the
two-reading rule (§3.8).

### 3.1 Inputs

| Argument | Meaning |
|---|---|
| `prev` | P as last written for this machine: this poller's, or the file another poller left (a takeover continues from it). None only when there has never been a P |
| `reading` | this cycle's `Reading` (architecture §6.2 plus §11.1's fields). A field that wasn't read this cycle is None |
| `expected` | E as read this cycle (None when missing or damaged). A newer-format E never reaches `judge` (§6.4 step 2) |
| `lock` | the **stale** lock the poller is polling past, or None. The poller never judges while a lock is fresh: it pauses (§6.4 step 3) |
| `machine`, `line`, `data` | the records from `DataService.current` |

### 3.2 Facts

`Facts` (§11.1) holds what the printer showed. `facts_from(prev, reading)` merges them: each field takes
the reading's value when it isn't None, else prev's. The exception is `message_name` in a standalone
reading that is reachable with `problem == ""`: it is always the reading's own (None when MSGNAM gave no
name this check), because standalone checks never load the message (C12). `prev_facts(prev)` builds the
facts from P alone.

| Fact | Kind | From | Notes |
|---|---|---|---|
| `reachable` | judged | `reading.reachable` | always the reading's |
| `problem` | judged | `reading.problem` | `""`, `"wrong_printer"` or `"login_failed"`; always the reading's when reachable |
| `printing` | judged | PRIN | |
| `running` | judged | web Load of slot 0 | None when `reading.running_reset` and no Load succeeded this cycle |
| `ink_level` | judged | INKL | 0 not detected, 1 normal, 2 low, 3 critical, 4 none |
| `clock_offset_s` | judged | DATE, TIME (§6.9) | |
| `daily` | judged | the last complete daily check (§6.10) | |
| `foreign_client` | judged | the poller's count (§6.8) | its changes commit at once (§3.8) |
| `running_unchecked` | judged | the poller's message check (§6.5) | True when the running message hasn't been read for long; its changes commit at once (§3.8) |
| `failures` | display | the poller | failed requests in a row |
| `message_name` | display | MSGNAM | `""` for unnamed; triggers a Load when it differs (§6.5) |
| `bag_count`, `bags_recent` | display | the bag counter (§6.7) | `bags_recent` feeds `was_printing` |
| `ink_name`, `head_temp_c` | display | INKT, PHTP | |

P stores the **committed** value of each judged fact and the **latest** value of each display fact
(`Judgement.facts`, §3.8). So `prev_facts(prev)` always means "what was last confirmed".

### 3.3 Matching the running message

```python
def norm_name(name: str | None) -> str:
    # None, "None" and whitespace are an unnamed message: ""; anything else stripped
def matches_expected(running: RunningMessage | None, *, name: str, slot: int | None,
                     fingerprint: str) -> bool:
    # running is not None; running.slot == slot when slot is not None;
    # norm_name(running.name) == norm_name(name); running.fingerprint == fingerprint when fingerprint != ""
```

1. "R matches E" is `matches_expected(R, name=E.message_name, slot=E.slot, fingerprint=E.fingerprint)`.
2. During `switch_pending`, "the new message" is the same call, and "the old message" is
   `matches_expected(R, name=E.previous_message_name, slot=None, fingerprint=E.previous_fingerprint)`.
3. R is `reading.running` when the cycle loaded it, else `prev.running`. The poller sets
   `reading.running_reset` when R may no longer describe the printer (after a lock, after E changed,
   after a worker start, after the printer was unreachable) and the Load failed, so a stale R is never
   used (§6.4).
4. Drift is decided only from web Loads, which carry their own request numbers. A MSGNAM that differs
   from the expected name only makes the poller load the running message at once (§6.5): `judge` sets
   `Judgement.read_running_now` when `reading.message_name` was read, the cycle made no Load, R is
   known, and `norm_name(reading.message_name) != norm_name(R.name)`.
5. The fingerprint leaves out the rendered date text (architecture decision 22; send.md's
   `fingerprint`).

### 3.4 Conditions

`classify(facts, prev, expected, lock, machine, line, data, *, acked, standalone, at) -> Outcome`
evaluates every condition below independently. Each true condition may give a **code** and an **alert
need** (kind, ref, colour, message, recipients). The outcome's code is the highest-precedence code
(§3.5). Its alerts are all the needs. Notation: F = facts, E = expected (a missing E is treated as mode
`none` with every flag false), L = the stale lock, `on` = trusted and `F.printing is True`, `off_read` =
trusted and `F.printing is False`, "carried" = §3.7.

| # | Condition | Code | Alert |
|---|---|---|---|
| C1 | `F.reachable` and `F.problem == "wrong_printer"` | `wrong_printer` | `setup_problem` |
| C2 | `F.reachable` and `F.problem == "login_failed"` | `login_failed` | `setup_problem` |
| C3 | not standalone, and (L.purpose in (`test`, `calibration`), or E.mode `test_running`): if `on`, or (not trusted and `prev.printing is True` and the `test_still_printing` alert isn't acknowledged) | `test_orphaned` | `test_still_printing`, ref = L.run_id, else `E.test["run_id"]` |
| C3b | the same, otherwise | `test_interrupted` | none |
| C4 | not standalone, and a Send was interrupted (§3.6) | `interrupted_*` | `send_interrupted`, ref = the interrupted send_id |
| C5 | E.mode `stop_requested_at_printer`: `on` gives `stop_failed`; not trusted, or `F.printing is None`, gives `stop_requested`; `off_read` gives nothing here | `stop_failed` / `stop_requested` | `stop_failed` |
| C6 | `E.failure` is set | `left_red` if `E.left_by`, else `failed_after_switch` or `failed_before_switch` by `E.failure.after_switch` | `send_failed`, ref = E.send_id |
| C7 | E.rejected and E.mode `running` and no E.failure | `rejected` | `send_rejected`, ref = E.send_id |
| C7b | E.rejected and no E.failure (any mode) | none | `send_rejected`, ref = E.send_id (the same need as C7; one alert) |
| C8 | `on` and E.mode in (`stopped_by_user`, `test_finished`). Carried while not trusted | `printing_on_unexpected` | `printing_on` |
| C9 | not `F.reachable`, and (prev's `went_silent` alert is active, or (`prev.reachable` and `was_printing(prev, data.company)`)), and that alert isn't acknowledged | `unreachable_printing` | `went_silent` |
| C10 | `off_read` and E.mode `running` and not E.rejected and not (`E.failure` and `E.failure.after_switch`), and (prev's `printing_off` alert is active, or (`prev.reachable` and `prev.printing is True` and `prev.problem == ""`)). Carried while not trusted | `printing_off` | `printing_off` |
| C10b | `off_read` and E.mode `running` and not E.rejected and no E.failure, and not C10 | `not_printing` | none |
| C11 | not standalone, trusted, R is not None, E.mode `running`, not E.rejected, not (`E.failure` and `after_switch`), E names a message (`E.message_name` or `E.fingerprint` not empty), and R doesn't match E. Carried while not trusted | `drift` | `drift` |
| C11e | not standalone, trusted, R is None (a required Load failed, §6.4), prev knows a drift (as C11c), `E.counter == prev.expected_counter`, and E as in C11 (owner decision, 31 Oct, second review). R unknown reads nothing about the message, so only a Load and its second reading end a known drift | none (C11b gives `message_unchecked`; the red alert keeps the view red, §5.2 item 2) | `drift` (prev's, the same id and acks) |
| C11c | standalone, prev knows a drift (`prev.state.code == "drift"` or an active `drift` alert in prev), `E.counter == prev.expected_counter`, E.mode `running`, not E.rejected, not (`E.failure` and `after_switch`), and E names a message (owner decision, 31 Oct). Not cleared in standalone, also while not trusted or acknowledged | when trusted: `drift`, or `unknown_while_down` when MSGNAM was read and names another message than R, or R is None | `drift` (prev's, the same id and acks) |
| C11d | standalone and `F.running_unchecked` (share mode's message check had failed: carried from the seed, §6.12 item 3; standalone never computes it), owner decision, 31 Oct, second review | when trusted, E.mode `running` and not E.rejected: `unknown_while_down` | `message_unreadable` (prev's, while it is active) |
| C11b | not standalone, trusted, E.mode `running` or `switch_pending`, not E.rejected, and (R is None, or `F.running_unchecked`). Carried while not trusted | `message_unchecked` | `message_unreadable` only while `F.running_unchecked` |
| C12 | standalone, E names a message, `reading.message_name` was read this cycle and `norm_name` differs from `E.message_name`; or standalone, trusted, E names a message and MSGNAM gave no name this check (`F.message_name` is None, §3.2); or standalone, trusted, E.mode `running`, no E.failure, not E.rejected and `F.printing` is None (PRIN not read since the outage began); or standalone and E.mode in (`switch_pending`, `test_running`) | `unknown_while_down` | none |
| C13 | `on` and `F.ink_level == 4`. Carried while not trusted | `ink_out` | `ink_out` |
| C14 | `on` and `F.ink_level == 0`. Carried while not trusted | `cartridge_error` | `cartridge_error` |
| C15 | trusted and (`F.ink_level` in (2, 3), or (`F.ink_level` in (0, 4) and not `on`)). Carried while not trusted | none | `low_ink` (text by level, §10) |
| C16 | E.rejected and not `on` and (E.mode `stopped_by_user`, or (E.mode `stop_requested_at_printer` and `off_read`)) | `stopped_needs_send` | none (C7b's alert continues) |
| C17 | not `on`, and (E.mode `test_finished`, or (E.stop_reason `test` and (E.mode `stopped_by_user`, or (E.mode `stop_requested_at_printer` and `off_read`)))) | `test_finished` | none |
| C18 | not `F.reachable` and not C9 | `off` | none |
| C19 | E.mode `running`, no E.failure, not E.rejected, not (`off_read`), and (R is not None or standalone) | `unconfirmed` if not E.confirmed; `confirmed_with_notes` if confirmed and E.notes; else `running_confirmed` | none |
| C20 | not E.rejected and E.stop_reason isn't `test` and not `on` and (E.mode `stopped_by_user`, or (E.mode `stop_requested_at_printer` and `off_read`)) | `stopped` | none |
| C21 | E.mode `none` (or no E): `on` gives `not_from_stripes`; otherwise `idle` | `not_from_stripes` / `idle` | `not_from_stripes` (no sound) when `on` and trusted. Carried while not trusted |
| C22 | trusted and `F.clock_offset_s` is not None and `abs(F.clock_offset_s) > timings.clock_warn_s`. Carried while not trusted | none | `clock_off` |
| C23 | `F.foreign_client` | none | `foreign_client` |
| C24 | `machine.placement == "encoder"` and the daily encoder values differ from the reference (§6.10) | none | `encoder_changed` |
| C25 | `F.daily.get("fonts")` is a non-empty list that differs from `data.company.standard_fonts` | none | `fonts_differ` |

Notes:

1. C6 also covers E.mode `none` with a failure (a first-ever Send that failed before switching).
2. C10's "read on before" makes the difference between "someone switched printing off at the printer"
   (red, sound) and "the printer came back from being off with printing off" (amber, no sound). See
   OPEN item 2.
3. C11 is skipped for a stopped machine on purpose: a message changed on a stopped printer can't print
   until someone switches printing on, which C8 catches, and Resume's own check refuses it (send.md).
4. A code always results: with E.mode `running`, an untrusted printer gives C18 or C9; a trusted one
   gives C10, C10b, C11b or C19 (C19 needs R in share mode; without R, C11b applies).
5. In standalone mode C3, C4, C11 and C11b are skipped and C12 applies (§6.12); C19 then doesn't need
   R, because standalone checks never load the message. A drift already known from the share is the
   exception (C11c, owner decision, 31 Oct): standalone checks can't see it end, and a same-name drift
   looks like E to C12, so it stays red with its alert, its acks and R (`effective_origin` shows R's
   origin while MSGNAM reads R's name) until a share-mode judgement clears it. When MSGNAM names
   another message (something else selected at the printer, E's own name included when R's differs),
   the best it gets is amber `unknown_while_down` with the origin unknown, and the drift alert stays
   open, so the view stays red (§5.2 item 2). So does a known drift whose R share mode couldn't read
   (C11e). Standalone never raises a drift of its own. A message share mode couldn't check
   (`message_unchecked`) is the other exception (C11d, owner decision, 31 Oct, second review):
   standalone can't check it either, so it is at best amber `unknown_while_down` with the origin
   unknown, never green on MSGNAM alone.
   C11e is the share-mode side (owner decision, 31 Oct, second review): a Load that fails leaves R
   unknown only when it was required (§6.4). That reads nothing about the message, so a known drift
   keeps its alert (the same id, acks kept) and the view stays red with the origin unknown; the next
   Load and its second reading decide. Without it, one failed Load would end a drift alert nobody
   acknowledged, and the next good Load would start a new one that rings again.
6. C11b exists because a running-message Load that keeps failing (a firmware update that changed the
   web page, or replies that never match) is a failed check (plan: "A failed or unrecognised read
   counts as a failed check"): without it a machine would stay green on its last good reading while
   drift isn't being checked. `running_confirmed` therefore needs R, and `effective_origin` shows the
   origin as unknown while the code is `message_unchecked` or P's `running_unchecked` is True, and
   whenever R is None while E.mode is `switch_pending`, even when a higher code (`failed_after_switch`,
   `left_red`, `interrupted_*`, `ink_out`) hides C11b (§5.3).

### 3.5 Precedence

`hx_state.PRECEDENCE`, highest first. The outcome's code is the first one in this order among the codes
the true conditions gave.

```python
PRECEDENCE = ("wrong_printer", "login_failed",
              "test_orphaned", "test_interrupted",
              "interrupted_unknown", "interrupted_old", "interrupted_new",
              "stop_failed", "stop_requested",
              "failed_after_switch", "left_red", "failed_before_switch",
              "rejected", "printing_on_unexpected",
              "unreachable_printing", "printing_off", "drift", "unknown_while_down",
              "ink_out", "cartridge_error",
              "stopped_needs_send", "test_finished",
              "off", "not_printing", "message_unchecked",
              "unconfirmed", "confirmed_with_notes", "not_from_stripes",
              "stopped", "idle", "running_confirmed")
```

Every alert need is kept whatever code wins, so a machine that failed a Send and then went silent shows
`failed_before_switch` with both `send_failed` and `went_silent` active.

### 3.6 Crash recovery: a stale lock combined with what's running

A Send's lock goes stale 60 s after its PC stops. The poller doesn't judge while the lock is fresh
(the machine is blue), and it polls past a stale lock, so `judge` gets L. Its first cycle past the
lock always loads the running message (§6.5).

C4 (interrupted Send) is true when, with `sid` = L.run_id (or E.send_id when there is no L):

- L.purpose is `send` and not (E.send_id == sid and E.failure) and not (E.send_id == sid and
  E.rejected) and not (E.send_id == sid and E.mode `running` and E.confirmed); or
- there is no L, E.mode is `switch_pending` and E.failure is None (the lock was removed but the final
  expected state was never written).

Then the code is:

| E compared with the interrupted Send | What the printer shows | Code | Colour |
|---|---|---|---|
| E.send_id ≠ sid: the Send stopped before recording "switch pending", so PRINTMSG was never sent | anything | `interrupted_old` | RED |
| E.send_id == sid, mode `switch_pending` | `on` and R is the new message | `interrupted_new` | AMBER |
| | `on` and R is the old message | `interrupted_old` | RED |
| | anything else: printing off, R neither, R unknown, not trusted | `interrupted_unknown` | RED |
| E.send_id == sid, mode `running`, not confirmed and not rejected (both running reads passed, the PC stopped before releasing) | `on` and R matches E | `interrupted_new` | AMBER |
| | anything else | `interrupted_unknown` | RED |

With `switch_pending` E carries the fingerprint of the idle-slot read-back (send.md §14), so "the new
message" means the new name in the expected slot with the checked content: a message that has the
new name but different content is neither new nor old, and gives `interrupted_unknown`.

`{computer}` is L.computer (else E.computer). The `send_interrupted` alert goes to the sender (L.user,
else E.sender) and the notify user, with sound; its colour is the code's colour. Plan wording:
red "Send interrupted on PC X" if the old message is still printing, amber "Send interrupted, not
confirmed" if the new one is.

Other stale locks:

| Stale lock purpose | Result |
|---|---|
| `test`, `calibration` | C3: `test_orphaned` (red, `test_still_printing` with sound to the notify user and the floor board: "Test message still printing on {machine}") when printing is on; `test_interrupted` (red, no sound) when it isn't. Resume is blocked; the machine needs a Send |
| `stop`, `resume`, `clock` | judged as usual against E, with the detail "A Stop on {computer} didn't finish." (or Resume, or clock set). A Stop that switched printing off before its PC stopped may raise `printing_off`; that is accepted (§15) |

How it ends: the stale lock stays until the next lock holder takes it over (storage.md §11.4). A new
Send makes the machine blue, then writes a new E, so the poller judges the new state. For
`interrupted_new` with E.mode `running`, the sender (`E.sender`) can also reopen the result from the
monitor card and confirm, even though the crashed PC never wrote the Send's log entry (send.md §11.2
and §11.3 fall back to the expected states); confirming takes the lock (taking over the stale one) and
releases it, so the machine becomes `running_confirmed`.

### 3.7 When the printer's facts can't be trusted

The facts aren't trusted when the printer isn't answering (`not F.reachable`), answers with another UID
(`wrong_printer`) or refuses the login (`login_failed`).

1. Conditions that need a fresh printer reading (C8, C10, C11, C11b, C13, C14, C15, C21's alert, C22)
   aren't evaluated.
   Each alert they had raised stays active with the same id ("carried"), unless it has been
   acknowledged, in which case it ends. When the printer is trusted again, the conditions are evaluated
   afresh from new readings and raise new alerts if the problem is still there.
2. Conditions on E alone (C5 to C7, C16, C17, C19, C20) still apply with `F.printing` treated as
   unknown.
3. `was_printing(prev, company)` is true when `prev.printing is True` and either
   `company.bag_counter == "none"` or `(prev.bags_recent or 0) > 0`. It decides between
   `unreachable_printing` (C9) and `off` (C18) at the moment the printer stops answering. Once raised,
   `went_silent` stays until the printer answers or the alert is acknowledged; then the machine shows
   `off`. "Machines are often just switched off" (plan): a printer that wasn't printing bags goes
   straight to grey `off` without an alert.
4. `reachable` turns False when `failures >= timings.unreachable_after` (3), counted by the poller
   (§6.6), and back to True on any reply.

### 3.8 The two-reading rule

The plan: "Any reading that changes the state or raises an alert is read again a second later and must
match." `judge` implements it like this:

```
F_new = facts_from(prev, reading);  out_new = classify(F_new, ...)
key(out) = (out.code, frozenset(f"{a.kind}:{a.ref}" for a in out.alerts))
prev_key = (prev.state.code, frozenset(active alert keys in prev))      # ("", {}) when there is no prev
1. key(out_new) == prev_key                      -> commit out_new
2. out_old = classify(prev_facts(prev), ...same E, L, acks...)
   key(out_old) == key(out_new)                   -> commit out_new   (E, L or an ack caused it)
3. only F.foreign_client or F.running_unchecked differ
                                                  -> commit out_new   (each already sums many requests)
4. not reading.second                             -> keep prev's state, alerts and judged facts;
                                                     recheck = evidence(prev_facts(prev), F_new);
                                                     candidate = out_new's snapshot
5. reading.second and (reading.pending_code, reading.pending_alerts) == key(out_new)
                                                  -> commit out_new
6. otherwise                                      -> keep prev; no recheck (the next cycle starts afresh)
```

Step 6 after a confirming Load that failed, or wasn't made (owner decision, 31 Oct): prev is kept, but
the clock isn't reset. The next cycle, about 5 s later, loads again (a retry, §6.4), and that Load is
judged by these same steps. The retry isn't a required Load, so when it fails too R stays as it was. The worker counts the failed confirmations in a row, and the third commits
amber `message_unchecked` ("can't check the message") until a Load and its second reading settle it
(§6.5, failed confirmations). No single reading gives `drift` or its alert: amber only says the check
failed.

"Commit" means: `Judgement.state` is the new snapshot (`since` = prev's when the code is unchanged,
else `reading.at`), `Judgement.alerts` is §4.2's list, `Judgement.facts` = F_new. "Keep prev" means:
state and alerts unchanged, and `Judgement.facts` = the judged facts from prev with the display facts
from F_new.

`evidence(old, new) -> list[str]` returns the items to read again:

| Fact that differs | Item | What the poller re-reads |
|---|---|---|
| `reachable` | `"reach"` | `read("PRIN")` |
| `problem` `wrong_printer` | `"uid"` | `read("HXUI")`, compared with `printer.uid` |
| `problem` `login_failed` | `"login"` | `login()` (a token that can't be unscrambled can't change; it commits at once) |
| `printing` | `"PRIN"` | `read("PRIN")` |
| `running` | `"load"` | `load_slot(0)` |
| `ink_level` | `"INKL"` | `read("INKL")` |
| `clock_offset_s`, only when it crosses `clock_warn_s` | `"clock"` | `read("DATE")`, `read("TIME")` |
| a `daily` value | `"daily:<TAG>"` or `"fonts"` | that read, or `fonts(deadline=...)` |
| `running_unchecked` | none | commits at once (rule 3 below) |

The second reading is taken `second_reading_s` (1 s) after the first ends, with the lock checked again
first. It is built from the first reading with only the rechecked items replaced, `second=True`,
`at` = now, and `pending_code`/`pending_alerts` copied from the first judgement's candidate. If the
list is empty (shouldn't happen), the change commits at once.

Examples: a single stray PRIN `0` from a mixed-up reply makes a candidate `printing_off`; the second
read gives `1`, so nothing changes. A confirm (E.confirmed becomes True) changes `unconfirmed` to
`running_confirmed` without a second reading, because the same printer facts give the same outcome
(step 2). After a Send, the poller's first Load shows the new message. With P's R (the previous
message) the new E gives `drift`, which differs from the outcome of the new R, so step 4 applies: a
second Load a second later must agree before `unconfirmed` commits. Meanwhile the view shows E's code
in amber with the origin unknown (§5.1 step 2.7, §5.3 rule 0c). If R doesn't match E (drift), the
second Load must agree too.

### 3.9 Headlines and details

`MachineSnapshot.headline = Msg(STATE_TEXTS[code], values)`. Values are plain record names, user names,
computer names and numbers, never bag text (architecture §4: bag text is shown in its own widgets).

| Code | Values | Details (`MachineSnapshot.detail`, in order) |
|---|---|---|
| `stopped` | `user`: E.stopped_by (E.written_by when it is `""`) | |
| `left_red` | `user`: E.left_by | |
| `rejected` | `user`: E.sender (the sender is the only one who can reject) | |
| `action_in_progress` (any other purpose) | `user`, `computer`: from the lock | |
| `test_interrupted`, `interrupted_old`, `interrupted_unknown` | `computer` | "Started by {user}." (L.user, else E.sender) |
| `test_finished` | | "After a test by {user}." |
| `failed_before_switch`, `failed_after_switch`, `left_red` | | `E.failure.msg` (send.md's message for the failed step) |
| `running_confirmed`, `confirmed_with_notes` | | "Confirmed by {user}." (E.confirmed_by) |
| `confirmed_with_notes` | | one per note in E.notes: `hx_message.NOTE_TEXTS[note]` (send.md §19.1), the same text the result window shows |
| `unconfirmed`, and `interrupted_new` while E.mode is `running` | | "Waiting for {user} to confirm." (E.sender) |
| `interrupted_new` while E.mode is `switch_pending` | | "Started by {user}. This machine needs a new Send." (L.user, else E.sender): nobody can confirm it (§5.3 `confirm_pending_by` is `""`, and it is in `needs_send`) |
| `message_unchecked` | | "Message last read {minutes} min ago." when `P.running_ok_at` is set (minutes since then, rounded down, at least 1), else "Stripes hasn't been able to read the message yet." |
| `off`, `unreachable_printing` | | "Not answering for {minutes} min." once it is at least 1 minute (`P.unreachable_since`) |
| any, with a stale `stop`/`resume`/`clock` lock | | "A Stop on {computer} didn't finish." / "A Resume on {computer} didn't finish." / "A clock set on {computer} didn't finish." |
| any, when `message_name` isn't empty | | "Message {name} in slot {slot}." from R |

## 4. Alerts

### 4.1 Kinds

`hx_state.ALERT_KINDS: dict[str, AlertKind]` (§11.1). "Roles" become `Alert.recipients` when the alert
starts (§4.3).

| Kind | Raised by | Sound | Colour | Roles | Ends when | ref |
|---|---|---|---|---|---|---|
| `send_failed` | C6 | yes | RED | sender, notify | E.failure is cleared or E.send_id changes | E.send_id |
| `send_rejected` | C7/C7b | yes | RED | sender, notify | E.rejected is cleared, E.failure is set, or E.send_id changes | E.send_id |
| `send_interrupted` | C4 | yes | the code's (RED, or AMBER for `interrupted_new`) | sender, notify | C4 is false (a new lock and E, or a confirm) | the interrupted send_id |
| `stop_failed` | C5 | yes | RED | last sender, notify, floor | printing read off, or E.mode changes | |
| `drift` | C11 | yes | RED | notify, last sender, floor | R matches again; acknowledged while not trusted | |
| `printing_off` | C10 | yes | RED | notify, last sender, floor | printing read on, E.mode isn't `running` any more; acknowledged while not trusted | |
| `printing_on` | C8 | yes | RED | notify, last sender, floor | printing read off, or E changes; acknowledged while not trusted | |
| `ink_out` | C13 | yes | RED | notify, last sender, floor | INKL isn't 4, or printing is off; acknowledged while not trusted | |
| `cartridge_error` | C14 | yes | RED | notify, last sender, floor | INKL isn't 0, or printing is off; acknowledged while not trusted | |
| `went_silent` | C9 | yes | RED | notify, last sender, floor | the printer answers; or acknowledged (the machine then shows `off`) | |
| `test_still_printing` | C3 | yes | RED | notify, floor | printing read off, or a new lock and E; acknowledged while not trusted | the test's run_id |
| `encoder_changed` | C24 | yes | RED | notify, last sender, floor | the values match the reference again (§6.10) | |
| `low_ink` | C15 | no | AMBER | notify, last sender, floor | INKL 1 | |
| `clock_off` | C22 | no | AMBER | notify, last sender, floor | the offset is back within `clock_warn_s` | |
| `foreign_client` | C23 | no | none | admins, monitor | §6.8 | |
| `setup_problem` | C1, C2 | no | none (the code is red already) | admins, notify | the code goes | |
| `fonts_differ` | C25 | no | none | admins, monitor | the list matches again | |
| `message_unreadable` | C11b | no | AMBER | notify, admins, monitor | a Load succeeds, or E.mode isn't `running`/`switch_pending` any more; acknowledged while not trusted | |
| `not_from_stripes` | C21 | no | none (the code is amber already) | notify | the first Send, or printing read off; acknowledged while not trusted | |

How the plan's alert table maps to these:

| Plan row | Here |
|---|---|
| A Send failed, or the operator said something's wrong | `send_failed`, `send_rejected` (sender and notify user, sound until acknowledged). The line stays red until a Send succeeds or someone presses Leave it (`left_red` stays red too; Leave it is logged by send.md) |
| Drift | `drift`; a printer running a message Stripes never sent at all (no E) gets the no-sound warning `not_from_stripes` instead (§15 decision 34) |
| Printing switched off without anyone pressing Stop, ink out, or a cartridge error | `printing_off`, `ink_out`, `cartridge_error` (and `printing_on` for the reverse, §15) |
| "A failed or unrecognised read counts as a failed check" (plan, web read) | `message_unreadable`, without sound, with the amber `message_unchecked` code |
| A printer went silent while it had been printing bags in the last few minutes | `went_silent` |
| A test message still printing after the test PC stopped | `test_still_printing` |
| Low ink, or a clock more than 2 minutes off | `low_ink`, `clock_off`, without sound |
| A Send not yet confirmed by the sender | not an alert: the amber `unconfirmed` code on the floor board and monitor |
| Replies that don't make sense | `foreign_client`, without sound, to administrators and the monitor |
| A printer that isn't answering and wasn't printing | not an alert: the grey `off` code |
| Nobody is polling a line | not an alert: `not_monitored` (§5.1) |
| A Send interrupted by a PC crash | `send_interrupted` |
| If stopping fails (plan, "When the operator says something's wrong") | `stop_failed` |
| Daily check: an encoder change "raises an alert" | `encoder_changed` |

### 4.2 Identity and lifetime

```python
def alert_id(machine_id: str, kind: str, *, ref: str = "", at: float = 0.0) -> str:
    # ref given:  f"{machine_id}.{kind}.{ref}" with ref's characters outside [A-Za-z0-9_-] replaced by "_"
    #             and cut to 24 characters
    # otherwise:  f"{machine_id}.{kind}.{int(at * 1000)}"     (at = the reading's wall time when it started)
    # e.g. "mc_5d1e2f3a4b.send_failed.sd_8a7b6c5d4e", "mc_5d1e2f3a4b.drift.1793275205123"
```

Every id matches storage.md's alert-id pattern `^[A-Za-z0-9._-]{1,100}$`. An id is unique for each
occurrence, so an old acknowledgement never silences a new alert (storage.md §5.6 rule 5).

`judge` builds `Judgement.alerts` from the outcome's needs:

1. A need whose `kind:ref` matches an active alert in prev **continues** that alert: same `id`,
   `started_at`, `recipients`, `acked_by`, `acked_at` and `waiting`; its `msg` and `colour` are
   refreshed. This also holds across a takeover, because the new poller's prev is the old poller's P.
2. A need without a match **starts** a new alert: `id` from `alert_id` (with `ref`, else `at =
   reading.at`), `started_at = reading.at`, `recipients` resolved now (§4.3), `active = True`.
3. An active alert in prev without a need **ends**: it is left out of `Judgement.alerts` (the list holds
   active alerts only) and appears in `Judgement.ended`, with `ended_at = reading.at`.
4. `acked_by`/`acked_at` are filled from `reading.acked` (the acks the poller found, §6.4 step 4).

Alerts are logged when they start, end and are acknowledged (§9). The event log is the history; P only
holds what is active.

### 4.3 Recipients

Roles are resolved once, when the alert starts, into `Alert.recipients`:

| Role | Becomes | Skipped when |
|---|---|---|
| sender | `"user:" + ` the sender (§0): `L.user` for `send_interrupted` with a lock, else `E.sender` (`E.written_by` when `E.sender` is `""`) | empty |
| notify | `"user:" + line.notify_user` | the line has no notify user |
| last sender | `"user:" + E.sender` | no E, or `E.sender` empty |
| floor | `"floor"` | |
| admins | `"admins"` | |
| monitor | `"monitor"` | |

User entries are de-duplicated with `hx_model.same_user`, keeping the first spelling. Every active alert
is shown on its machine's card on every monitor whatever its recipients; recipients decide who hears it,
who gets the personal pop-up (ui.md), and which administrator warnings are listed for administrators.

### 4.4 Sound: who rings, and the cadence

1. `sounding_alerts(board, identity, *, floor_board) -> list[Alert]` returns, de-duplicated by id and
   oldest first, every alert in `board` that is active, has `sound`, has no `acked_by`, and either names
   this user (`"user:<name>"` with `same_user(name, identity.user)`) or has `"floor"` while
   `floor_board` is True (this PC has the floor board open).
2. No sound alert is addressed to `"admins"`, so `sounding_alerts` needs no company record.
3. ui.md's `AlertSounder` runs whenever the services run, including when Stripes is hidden in the tray:
   a notify user signed in anywhere hears their line. It calls `hx_win.play_alert(repeat=True)` when
   the list becomes non-empty and `hx_win.stop_alert()` when it becomes empty. It re-evaluates on every
   `BoardUpdate` (every `watch_s`) and at once after this PC acknowledges.
4. Cadence: `assets/alert.wav` is looped with `SND_LOOP`. The file must last exactly 3.0 s: a two-tone
   chime of about 0.8 s (a higher tone, then a lower one) followed by silence, so the sound repeats
   every 3 s until every sounding alert is acknowledged or has ended. A new alert while it is already
   ringing doesn't restart it. There is no snooze. ui.md makes the file to this description.
5. Warnings (`sound` False) are never heard. They show on cards, on the floor board (when they colour
   the machine, §5.2) and in the alert list.

### 4.5 Acknowledging

1. Who: `can_acknowledge(identity, company)` is `identity.is_sender(company) or
   identity.is_admin(company)`. Stripes MapleJet Senders and Stripes Administrators may acknowledge any
   alert, sound or not.
2. How: the UI calls `StatusWatcher.acknowledge(alert_ids)` (§7). It never blocks. The watcher applies
   the acks to this PC's board at once (posting a new `BoardUpdate` straight away, so this PC goes quiet
   immediately) and writes one `Ack(alert_id, by=identity.user, computer=identity.computer,
   at=clock.time())` per id with `ShareStore.write_ack` on its own thread. The first ack wins (storage.md
   §5.6). The card's Acknowledge button acknowledges every active, unacknowledged alert of that machine;
   a line card's button does the same for every machine of the line.
3. Propagation: every PC's watcher reads the ack file of each active alert by name every `watch_s`
   (5 s), so the sound stops on every PC within about 5 s (up to about 10 s if the SMB not-found cache
   hides the new file; OPEN item 6). The assigned poller also reads them (§6.4 step 4), copies `acked_by`
   and `acked_at` into the alert in P, and logs `alert_acknowledged` once.
4. What it changes: only the sound, plus the two "acknowledged while not trusted" endings in §4.1. A
   red mark stays while the problem is still there: the code and the alert stay active.
5. Share down: an ack is applied on this PC only and isn't written (the alert ids of standalone checks
   are this PC's own, §6.12). Acks that fail to write while the share is up are retried by the watcher
   every `watch_s` for 10 minutes, then dropped.

### 4.6 Recipients who aren't signed in

1. An alert waits for its recipients: it stays active and unacknowledged in P until the problem ends or
   someone acknowledges it. When a recipient signs in to a PC running Stripes, that PC's watcher reads P
   within `watch_s` and its `AlertSounder` rings.
2. The wait is logged by the poller that owns the alert (§6.15): for each active, unacknowledged alert
   with sound and each `"user:<name>"` recipient, if no live heartbeat (`ShareStore.live_heartbeats()`)
   has `same_user(hb.user, name)`, it logs `alert_waiting` once and adds the name to `Alert.waiting`.
   When such a heartbeat appears while the alert is still active and unacknowledged, it logs
   `alert_reached` once (with the computer) and removes the name.
3. A heartbeat of another format doesn't count (storage.md §12.2): an old Stripes can't show MapleJet
   alerts, so its user really is still waiting.

## 5. Views

All view functions are pure and run on the watcher thread. Ages are seconds on this PC's own monotonic
clock (`ShareStore.poll_age`, `lock_age`, or the watcher's own `CounterWatch` in standalone mode).

### 5.1 machine_view

`machine_view(machine, poll, poll_age_s, expected, lock, lock_age_s, acks, timings) -> MachineView`:

1. `monitored = poll is not None and poll_age_s is not None and poll_age_s < timings.not_monitored_s`.
2. The code is the first that applies:
   1. **Fresh lock** (`lock` and `lock_age_s is not None and lock_age_s < timings.lock_stale_s`): by
      `lock.purpose`, `send` gives `switch_pending` when E.mode is `switch_pending`, else
      `send_in_progress`; `test` and `calibration` give `test_running` (step and of from `lock.note`,
      else `E.test`); `stop`, `resume`, `clock` give `action_in_progress` with that purpose's headline
      (`STATE_TEXTS["action_in_progress:stop"]` ...); any other purpose (a damaged lock reads as
      `unknown`, storage.md §11.7; a newer version may use a new one) gives `action_in_progress` with
      `STATE_TEXTS["action_in_progress:other"]`, "Printer in use: {user} on {computer}". Values `user`
      and `computer` come from the lock (a damaged lock has `""` for both).
   2. **Newer format**: P's state code is `update_stripes` (the watcher's placeholder for a newer P or
      E, §7): `update_stripes`.
   3. **No status**: `poll is None`: `checking` while `poll_age_s` is None or below `not_monitored_s`,
      else `not_monitored`.
   4. **Stale status**: `poll_age_s >= not_monitored_s`: `not_monitored`.
   5. **E not judged yet**: `expected` is not None, `expected.counter != poll.expected_counter`, and
      `expected_code(expected)` isn't None: that code. This covers the few seconds between a sender's
      write and the poller's commit of its first Loads of the new message.
   6. Otherwise `poll.state.code` (`checking` if `poll.state` is None).
   7. **Never green on an R older than E** (an owner-approved divergence, 31 Oct): when the code from
      steps 5 or 6 is GREEN, E isn't judged yet (`expected.counter != poll.expected_counter`) and R
      doesn't match E (or there is no R), the code is `confirmed_with_notes` when E is confirmed, else
      `unconfirmed` (both AMBER), with that code's headline. A sender who presses "Everything is right"
      before the poller has read the new message back therefore sees amber "Printing, not fully
      checked" with the origin unknown (§5.3 rule 0c) until the poller commits, never green with the
      previous message's origin. Green needs a Load since E changed (§15 decision 36).
3. Colour and headline: §5.2.

```python
def expected_code(e: ExpectedState) -> str | None:
    # failure: "left_red" if e.left_by else "failed_after_switch"/"failed_before_switch"
    # rejected: mode "running" -> "rejected"; "stopped_by_user" -> "stopped_needs_send";
    #           "stop_requested_at_printer" -> "stop_requested"
    # mode "running": "running_confirmed" / "confirmed_with_notes" / "unconfirmed" (as C19)
    # "stopped_by_user" -> "stopped" ("test_finished" when e.stop_reason == "test");
    # "stop_requested_at_printer" -> "stop_requested"; "test_finished" -> "test_finished"
    # "none", "switch_pending", "test_running" -> None (only the poller can tell)
```

### 5.2 Colour and headline

1. `code_colour = STATE_CODES[code]`, except `not_monitored`: RED or AMBER when the last known colour
   (P.state's colour, or `expected_code`'s colour when that is worse) was RED or AMBER, else GREY.
   NOT MONITORED never makes a machine look better than it last was; it only replaces green, blue and
   grey. Standalone mode keeps the same rule for an origin mismatch (owner decision, 31 Oct): a machine
   last known to print something other than E stays red with its drift alert and R's origin, and at
   best turns amber `unknown_while_down`, until share mode judges it (§3.4 C11c, §6.12 item 3). So does
   a known drift whose R a failed Load left unknown (C11e), with the origin unknown. A message share
   mode couldn't check (`message_unchecked`) stays at best amber `unknown_while_down` with the origin
   unknown (C11d, second review, 31 Oct): standalone has no Loads, so it can't check it either. Other
   states start afresh in standalone, as before.
2. Alerts colour the machine: an active alert with colour RED makes the machine RED whatever the code;
   an active alert with colour AMBER turns a GREEN or GREY code AMBER (never a BLUE, AMBER or RED one).
   Alerts without a colour change nothing. Acknowledged alerts still count.
3. `headline`: the code's headline, unless an alert changed the colour; then the msg of the first
   alert with that colour (sound before no sound, then oldest), and the code's headline becomes the
   first detail.
4. `worst(colours) -> str` uses the order RED, AMBER, BLUE, GREY, GREEN (worst first); an empty list
   gives GREY.

### 5.3 The other MachineView fields

| Field | Value |
|---|---|
| `machine_id`, `line_id`, `name` | from the record |
| `colour`, `code`, `headline` | §5.1, §5.2 |
| `details` | the replaced headline (§5.2), then `poll.state.detail`, then the msg of every other active alert, then "Waiting for {user} to sign in." for each name in an alert's `waiting` |
| `origin_line` | `effective_origin(poll, expected)` (below) |
| `message_name` | R's name, else `poll.message_name`, else `""` |
| `checked_age_s` | `poll_age_s` (None when never seen) |
| `monitored` | §5.1 step 1 |
| `printing` | `poll.printing` when monitored, else None |
| `alerts` | P's active alerts, each with `acked_by`/`acked_at` filled from `acks` when P doesn't have them yet |
| `unacked_sound` | any alert with sound and no `acked_by` |
| `can_stop` | not BLUE, monitored, `poll.reachable`, `poll.printing is True` |
| `can_resume` | not BLUE, monitored, `poll.reachable`, `poll.printing is False`, code in (`stopped`, `not_printing`, `printing_off`), and E has a `send_id`, is `confirmed`, isn't `rejected` or `resume_blocked`, and has no `failure`. send.md's `check_resume` then makes the real check against the printer |
| `needs_send` | code in `NEEDS_SEND_CODES` = (`failed_before_switch`, `failed_after_switch`, `left_red`, `rejected`, `stopped_needs_send`, `test_finished`, `test_interrupted`, `test_orphaned`, `interrupted_old`, `interrupted_unknown`, `drift`, `printing_on_unexpected`, `not_from_stripes`, `idle`), or `interrupted_new` while E.mode is `switch_pending` |
| `confirm_pending_by`, `confirm_send_id` | E.sender (E.written_by when it is `""`) and E.send_id when E.mode is `running`, not confirmed, not rejected, no failure and `send_id` set (the code is then `unconfirmed`, `interrupted_new`, `message_unchecked` or a red that an alert caused); else `""`. A clock set or Leave it by someone else never moves it, because they don't change `E.sender` |
| `test` | `{"by": lock.user, "step": ..., "of": ...}` for `test_running`; `{"by": E.test["by"], ...}` for `test_orphaned`, `test_interrupted` and `test_finished` when `E.test` is set; else None |
| `has_preview` | `poll.running` is set and its `preview_sha256` isn't empty |

```python
def effective_origin(poll: PollStatus | None, expected: ExpectedState | None) -> str:
    # 0. If poll and (poll.running_unchecked or (poll.state and poll.state.code == "message_unchecked")):
    #    ""  (unknown: the message can't be read, so neither the last reading nor E is shown as what
    #    prints, also when a higher code such as ink_out or rejected hides message_unchecked)
    #    Also "" when poll isn't standalone and its code is unknown_while_down (a standalone finding
    #    carried into share mode by a worker's start, §6.3 step 1). machine_view shows "" whenever its
    #    code is `checking` (§2.1), and `printing` None then. And "" when poll.running is None and
    #    known_drift(poll) (a known drift whose R a failed Load left unknown, §3.4 C11e and C11c: never
    #    E's origin. Owner decision, 31 Oct, second review)
    # 0b. If (poll is None or poll.running is None) and expected and expected.mode == "switch_pending":
    #    ""  (unknown: the new message was never read back running by the poller (a failure after the
    #    switch, Leave it, an interrupted Send, standalone), so E's origin is not shown as what prints,
    #    whatever code won)
    # 0c. If R = poll.running is set, expected is set, expected.counter != poll.expected_counter (E not
    #    judged yet) and R doesn't match expected: ""  (unknown: R was read before E was written, so
    #    right after a Send it is the previous message; E changes that keep the message, such as a Stop,
    #    a clock set or a Send that failed before the switch, still match and keep R's origin. Owner-
    #    approved divergence, 31 Oct)
    # 0d. If R and poll.standalone and (poll.message_name is None or norm_name(poll.message_name) !=
    #    norm_name(R.name)): ""  (an origin mismatch's R carried into standalone, §3.4 C11c, §6.12 item
    #    3: while MSGNAM names R's message, rule 2 shows R's origin, what prints, never E's; once MSGNAM
    #    names another message, or wasn't read, the origin is unknown. Owner decision, 31 Oct)
    # 1. R = poll.running. If R and expected and expected.fingerprint != "" and R.origin_line == ""
    #    and R matches expected: expected.origin_line (the pre-printed case: "USA (pre-printed)" has
    #    no origin item on the printer, and the fingerprint proves the content is the one sent)
    # 2. If R: R.origin_line, what the printer read back ("" when the message has no origin item).
    #    This holds even when R has the expected name, so after a failed content check or Leave it
    #    the card shows what is really printing
    # 3. If expected and poll and poll.message_name is not None and
    #    norm_name(poll.message_name) != norm_name(expected.message_name): ""  (unknown)
    # 3b. If expected and poll and poll.standalone and poll.reachable and poll.problem == "" and
    #    poll.message_name is None: ""  (unknown: the standalone check couldn't read the name, §3.2)
    # 4. expected.origin_line if expected, else ""
```

`origin_line` is bag text: the UI shows it in `BagText`, and shows "(origin unknown)" in its own
translated label when it is `""` (ui.md).

### 5.4 line_view

`line_view(line, machines, status) -> LineView`. `status` (the line's last Send) is informational and
never changes a colour (architecture decision 24).

1. `machines` keeps the given order (board_view passes them sorted, §5.5).
2. `origins_differ`: the set of `origin_line` values of the machines with `printing is True` has more
   than one member (`""` counts as its own value).
3. `colour = worst(m.colour for m in machines)`; GREY when there are none. When `origins_differ` and
   the colour would be GREEN, it is AMBER.
4. `monitored = bool(machines) and all(m.monitored for m in machines)`.
5. `checked_age_s`: the largest of the machines' ages that aren't None, else None.
6. `headline`:
   - no machines: "No machines";
   - no machine monitored: "NOT MONITORED";
   - every machine has the same code: that machine's headline;
   - `origins_differ` and the colour came from rule 3's AMBER: "Different origins on this line";
   - otherwise: `Msg("{machine}: {state}", {"machine": m.name, "state": m.headline})` for the worst
     machine (the first by name among equals).

### 5.5 board_view

`board_view(data, polls, poll_ages, expected, locks, lock_ages, lines, acks, *, share_ok, standalone,
now, timings) -> BoardView`:

1. Lines: the active lines of `data`, sorted by `natural_key(line.name)`. Machines: `data.line_machines
   (line_id)` (active), sorted by `natural_key(machine.name)`. `polls`, `poll_ages` and `expected` are
   keyed by machine id; `locks` and `lock_ages` by printer id; `lines` by line id; `acks` by alert id.
2. A machine whose printer record is missing, inactive or has `token == ""` gets a view with code
   `not_set_up`, colour GREY, no alerts and no buttons.
3. Every other machine gets `machine_view(...)`, then each line gets `line_view(...)`.
4. `BoardView(lines, share_ok, standalone, next_check_s, generated_at=now)`. `next_check_s` is set by the
   watcher from `PollerUpdate` (it isn't an argument here).

```python
def natural_key(name: str) -> list:   # "Line 2" before "Line 10": digit runs as ints, the rest casefolded
```

### 5.6 floor_board_rows

`floor_board_rows(board) -> list[FloorRow]`: one row per machine, in the board's order (line by name,
then machine by name). This fixed order lets people learn where their line is; rows never jump around
when a state changes.

| FloorRow field | Value |
|---|---|
| `line_id`, `machine_id`, `line_name`, `machine_name` | from the views |
| `colour` | the machine's colour; AMBER instead of GREEN when its line's `origins_differ` and this machine is printing |
| `origin_line` | the machine's `origin_line`, on every row. Rows of a line whose origins differ therefore list each machine's origin |
| `status` | the machine's headline; "Different origins on this line" instead when the colour was raised to AMBER here |
| `checked_age_s` | the machine's |

### 5.7 Other helpers

```python
def checked_text(age_s: float | None) -> Msg
    # None -> "Not checked yet"; < 120 -> "checked {seconds} s ago" (int, rounded down);
    # otherwise "checked {minutes} min ago" (int(age_s // 60))
def board_banner(board: BoardView) -> Msg | None
    # standalone: "Server unreachable: showing this PC's own checks · next check in {seconds} s"
    #             (seconds = int(board.next_check_s or 0))
    # share down and not standalone: "Server unreachable: this PC isn't checking any printers."
    # otherwise None
```

The UI adds the seconds since it received the `BoardUpdate` to every `checked_age_s` before calling
`checked_text`, so "checked N s ago" counts up between updates.

## 6. The poller

### 6.1 Threads and modes

`Poller` (architecture §6.8) runs the coordinator thread `hx-poller` and one worker thread
`hx-poll-<printer_id>` per printer it polls. Every PC running Stripes runs it, hidden in the tray too.

| `mode` | When | What it does |
|---|---|---|
| `"share"` | the share is available | heartbeats, assignment, a worker per printer this PC is rank 0 for (or has taken over) |
| `"standalone"` | the share is unavailable and the monitor or floor board is open on this PC (`set_monitor_open`) | quick checks of every active printer every `standalone_poll_s`, results in memory only (§6.12) |
| `"idle"` | no data yet, or share down with no monitor open | no printer requests |

The coordinator posts `PollerUpdate(mode, polling, next_check_s)` on the bus whenever any of the three
changes, and every second in standalone mode (for the countdown).

Module constants in `hx_poller`:

```python
FOREIGN_MIN = 3                # nonsense or busy replies ...
FOREIGN_WINDOW_S = 600.0       # ... within this long raise foreign_client
FOREIGN_CLEAR_S = 1800.0       # cleared after this long without any
MAX_BAGS_PER_S = 5.0           # a bag counter may rise by at most this per second (+10)
DAILY_S = 86_400.0             # daily checks
DST_KEEP_S = 86_400.0          # a pending daylight-saving clock set is kept this long
ACK_RETRY_S = 600.0            # the watcher retries a failed ack write this long
UNCHECKED_LOADS = 2            # failed running-message Loads in a row before running_unchecked ...
UNCHECKED_SLOW_SETS = 3        # ... or this many poll_slow_s without a successful one (§6.5)
```

### 6.2 The coordinator loop

Every `heartbeat_s` (5 s), waiting with `clock.sleep(heartbeat_s, cancel)`:

1. `data = DataService.current`. None: mode `idle`, nothing else.
2. Printers to poll: the active printers with a token of the active machines of active lines
   (`printer_ids`). A printer belongs to one machine (architecture decision 11).
3. Share mode (`store.available` and share-mode polling isn't suspended by §6.13):
   1. `store.heartbeat().beat(can_poll=reach.can_poll(), last_printer_reach=reach.last_printer_reach,
      polling=<printers with a running worker>, monitor_open=..., floor_board=...)`.
   2. `live = store.live_heartbeats()`; `ranks = hx_share.assign(printer_ids, live)`.
   3. For every printer, `read_poll(machine_id)` (kept for `seen_polls()`, §7), and then:
      - a running worker whose PC is no longer rank 0 while rank 0's heartbeat is live: tell it to hand
        over (storage.md §12.6 rule 4: finish the request in flight, write P with `yielded_to = <rank 0
        key>`, stop);
      - no worker, this PC at rank 0: start one when P names this PC as `poller`, or names it in
        `yielded_to`, or `poll_age >= stale_s` (the counter unchanged, or the file missing, for
        `stale_s`; storage.md §12.6 rule 2);
      - no worker, this PC at rank r ≥ 1: start one when `poll_age >= hx_share.takeover_after_s(r)`
        (15 s, 20 s, 25 s, …);
      - never start when P is fresh and names another poller that hasn't yielded to this PC (the
        invariant, storage.md §12.6 rule 1).
   4. Every `poll_slow_s`: `read_expected` of every active machine into the standalone cache (§6.12).
   5. The reachability probe (storage.md §12.3): when `reach.probe_due(unpolled=<some printer's P is
      missing or stale for stale_s>)`, pick the target by storage.md's rule (only a printer with no
      live poller and no lock) and call `clients.client(printer).probe()` (protocol.md §9 item 7: it
      never waits for the gate and sends nothing). `True`: `reach.reached()` and `reach.probed(True)`;
      `False`: `reach.probed(False)`; `None` (the gate was busy): neither.
   6. The waiting check of §6.15.
   7. Daylight saving: the current change (§6.11 item 1), handed to the workers in memory.
4. Standalone or idle: §6.12, §6.13.
5. `stop()`: set the cancel token; each worker finishes its request in flight and, in share mode,
   yields its printer to its next-ranked live poller (storage.md §12.6 rule 5); then
   `heartbeat().remove()`. Returns within `timeout_s` plus at most one request timeout.
6. The time thread `hx-time` (architecture §7.1), started by `start()` and stopped by `stop()`: at
   once and then every `TimeSource.cache_s` (600 s), `time.set_hosts([...])` from `data.current` and
   `time.now(fresh=True)`, waiting with `clock.sleep(cache_s, cancel)`. It is the only poller thread
   that ever waits on the time server; the coordinator and the workers only call `time.cached()`
   (send.md §9.2), which never blocks.

### 6.3 Starting a worker

1. Write P naming this PC: `counter` = the file's counter + 1 (1 when missing), `poller` set by the
   store, `paused_for_lock = ""`, `yielded_to = ""`, and every other field copied from the file (so
   state, alerts, facts, `daily` and `dst` continue). Exception: when `poll_age` at the start is
   `silent_window_s` or more (the old poller stopped long ago), `bags_recent` is written as 0, because
   those bags are long past and must not make a printer that is now off look as if it went silent while
   printing. Log `polling_started` with the reason (`assigned`, `takeover`, `handover`, `restart`).
   Second exception (owner-approved divergence, 31 Oct): after a long gap, when `poll_age` is
   `not_monitored_s` or more, or P's `written_at` is that old by this PC's clock (every PC closed
   overnight, where this PC has never seen the counter), nobody has judged the printer for that long,
   and the copied state under a counter that moves again would show as just checked: a green
   "Printing" with the origin from before an outage while the printer prints another one. So unless
   P's state is RED or AMBER (kept as it is), the start writes the state `checking` (GREY, "Checking...",
   origin unknown, §5.3), or, when this PC's last standalone check of the machine (§6.12 item 7) was RED
   or AMBER, that check's state snapshot (only share mode clears those). Alerts and facts are copied as
   usual, so judge still needs its evidence: unchanged facts commit at the first cycle (§3.8 step 2), a
   change needs its second reading.
2. Settle (storage.md §11.6): keep writing P every `poll_fast_s` (counter + 1, nothing else changed),
   and make the first request only after a lock read taken at least `hx_share.settle_s()` (6 s) after
   step 1's write still shows no lock. Then, right before the first request, the **rank check**: the
   worker computes `ranks = hx_share.assign(printer_ids, store.live_heartbeats())` itself (a fresh
   read, not the coordinator's last one). If this PC isn't rank 0 for the printer and rank 0's
   heartbeat is live, it makes no request: it writes P with `yielded_to = <rank 0 key>` (counter + 1),
   logs `polling_stopped` with reason `rank` and exits. Rank 0 carries on even if P now names another
   poller. This settles the start race: rank 0 starts when P is missing or stale for `stale_s`, and
   rank 1 at `takeover_after_s(1)`, also 15 s, so on a first start, a new printer, or a rank-0 PC whose
   heartbeat and P went stale together, two coordinators can start workers in the same moment; the
   rank-1 one yields here, after its settle and before any request. A `ShareUnavailable` from
   `live_heartbeats()` makes no request either (§6.13).
3. The first cycle reads HXUI and loads the running message (§6.5). The message check (§6.5)
   continues from P: `load_ok_mono` = the moment of step 1's write, and `loads_failed` =
   `UNCHECKED_LOADS` when `P.running_unchecked` is True (so `message_unreadable` keeps its id across a
   takeover, §15 decision 11), else 0. This first Load is a required one (§6.4): if it fails, the
   reading has `running_reset = True`, so an R inherited from P is never used to judge green.

### 6.4 The worker cycle (share mode)

Ticks every `poll_fast_s` on the monotonic clock (a tick that overran starts the next at once; missed
ticks aren't caught up). **P is written at every tick** the share allows, whatever happens, with
`counter` + 1, so the counter shows the poller is alive even while it is paused or backing off. Each
tick:

1. **Still mine?** If the coordinator asked this worker to hand over or stop, do that and exit.
   Otherwise the rank check of storage.md §12.6 rule 6, made by the worker itself at the start of
   every cycle: `hx_share.assign(printer_ids, live)` with the coordinator's latest `live` list (§6.2
   item 3.2, kept in memory; the start of §6.3 step 2 reads a fresh one). If this PC isn't rank 0 for
   the printer while rank 0's heartbeat is live, hand over now (as §6.2 item 3.3's first bullet: write
   P with `yielded_to = <rank 0 key>`, log `polling_stopped` with reason `rank`) and exit, without
   waiting for the coordinator's next tick.
2. **E**: `store.read_expected(machine_id)`. `FormatTooNew`: write P with state
   `MachineSnapshot(code="update_stripes", colour=GREY, ...)` and no alerts, make no request, end the
   tick. `ShareUnavailable`: §6.13, end the tick.
3. **Lock** (storage.md §11.6): if the printer is in `store.local_locks`, or `read_lock` shows a fresh
   lock, make no request: write P with `paused_for_lock` = the token (`"local"` for a local lock with no
   file), state, alerts and facts unchanged, and remember `lock_seen = True`. End the tick. A stale
   lock is L for this cycle.
4. **Acks**: `read_ack(id)` for each active alert in P without `acked_by`, into `reading.acked`.
5. **Backoff**: when P says unreachable and the next attempt isn't due (§6.6), make no request. If E's
   counter or the acks changed, judge with a Reading that holds only `reachable=False`, the failure
   count and the acks (that judgement can only change through E, L or acks, so it commits at once,
   §3.8 step 2). Write P, end the tick.
6. **Plan** the requests (§6.5).
7. **Read**, one request at a time, through the `PrinterClient` from `ClientPool`. `p_ok` is the
   `clock.monotonic()` taken just before the `write_poll` call that last succeeded (another PC counts
   from when it saw the counter change, which can be any moment during that call), and `end = p_ok +
   stale_s / 2 + http_timeout_s` (9.5 s after it) is the latest any request may still be running.
   Right before each request, in this order:
   1. **Check the lock** (`local_locks`, then `read_lock`). A fresh lock: abandon the cycle without
      judging and go to step 3's paused write. A `read_lock` that took longer than `stale_s / 4`
      (3.75 s) is not used: make no further request this cycle (and count it for §6.13).
   2. **Keep P fresh**, with `now` read after the lock read returned (and again after any P write). A
      `read` or `command` fits when `now + worst_case_s(call) <= end` (protocol.md §8.3); a web call
      (`load_slot`, `fonts`, `firmware`) fits when `now + http_timeout_s <= end`, because it gets
      `end` as its deadline. The worker never calls `check_uid()` or `read_twice()` (their worst case
      is longer); it reads `read("HXUI")` and lets the two-reading rule confirm a mismatch (§3.8).
      When `now − p_ok >= stale_s / 2` (7.5 s) already as this step 7 starts (the PC slept or was
      paused in the middle of the cycle), P hasn't proved this worker alive meanwhile, and another PC
      may have taken the printer over. So before step 7.1 (whose paused write is a P write too) the
      worker reads P: when another PC has written it since this worker's last write and hasn't
      yielded to it, the worker makes no request and no P write, ends the cycle without judging,
      continues from that PC's P, and polls again only once that PC yields to it (§6.2 item 3.3) or P
      has been quiet for `stale_s` (a live PC that ranks above this one is handed over to at once, and
      one this PC doesn't see as live is yielded to). The same check comes before the cycle's own
      write (step 11) and before a clock set records its TD (§6.11 item 3.3). Otherwise, if
      `now − p_ok >= poll_fast_s`, or the request doesn't fit, write P first (counter + 1, everything
      else as last written), which moves `p_ok` and `end` on. If it still doesn't fit (a failed or
      slow write), make no further request this cycle; and once `now − p_ok >= stale_s / 2`, apply §6.13:
      P can no longer prove this worker is alive, so another PC may soon start polling the printer.
      Nothing (no share operation, no sleep) runs between this check and the call.
   3. **Send it** at once. Every call gets `deadline=end`: `read(tag, deadline=end)`, `command(cmd,
      deadline=end)`, `load_slot(0, deadline=end)`, `fonts(deadline=end)`, `firmware(deadline=end)`.
      A call that has to wait for this PC's gate (a Stop, Resume or clock set on this PC took it
      between the lock check and the call) never waits past `end`, and one that gets the gate too
      late to finish by `end` sends nothing (protocol.md §9 item 2): `PrinterBusy`, which isn't a
      failure (§6.6), or the web call's `WebReadFailed`.
   With the default timings no request can then still be running 9.5 s after the start of the last
   good P write, while other PCs wait at least `stale_s` (15 s) of unchanged counter before taking over
   and a sender waits `stale_s` before treating P as stale. A lock read or P write that takes longer
   than `stale_s / 4` (3.75 s) counts as a failed one for §6.13, even when it succeeds. Exceptions map
   as in §6.6.
8. **Judge**: build the `Reading` and call `judge(P, reading, E, L, machine, line, data, timings)`.
9. **Load now**: if `judgement.read_running_now`, the cycle had no Load, and no such extra Load was
   made in the last 30 s for this printer, sleep `second_reading_s`, then `load_slot(0)` by step 7's
   rules (the lock checked, P kept fresh, the deadline), and judge again as a first reading. (The
   30 s limit keeps a printer whose MSGNAM and Load names disagree for good within the budget.)
10. **Second reading**: if `judgement.recheck`, sleep `second_reading_s` (cancellable), re-read the
    items (§3.8) by step 7's rules, and judge the second reading.
11. **Write**: if `running.preview_sha256` changed, `write_preview` first (storage.md §5.3), then
    `write_poll` with §6.14's fields.
12. **Log** `state_changed` when the code changed, and `alert_started`, `alert_ended`,
    `alert_acknowledged` from the judgement (§9).
13. **Reach**: `reach.reached()` when any request got a reply this cycle.

R is reset as §3.3 says: when the cycle follows a paused tick (`lock_seen`), sees an E counter
different from `P.expected_counter`, is the first cycle after a worker start (§6.3), or is the first
cycle after the printer was unreachable, it must load the running message; if that Load fails, the
reading has `running_reset = True`.

A failed Load is tried again at the next cycle (owner decision, 31 Oct). Any Load that fails (a slow
set's, a required one, a `read_running_now` one), and a confirming Load (a second reading's `"load"`,
§3.8) that fails or isn't made because an earlier item of the second reading ended its requests, sets
`retry_load`: the next cycle, about 5 s later, makes a Load instead of waiting for the next slow set.
That Load is judged by the usual two-reading rule. The retry isn't a required Load (second review,
31 Oct): when it fails too, R stays as it was, as for a slow set's Load, so a committed drift keeps its
R, its code and its alert (same id, acks kept); only `need_load` (the R-reset cases above) makes a
Load required. A confirming Load that wasn't made
confirms nothing: the second reading never keeps the first Load's R. The retries stop once the
message check counts as failed and that judgement has committed (amber `message_unchecked`, §6.5);
Loads then follow the slow set again, so the budget holds. One request at a time still applies: the
retry is the next cycle's own Load.

An exception that isn't an `HxError` (a bug) is caught at the top of the tick, printed with its
traceback, logged as `internal_error`, and P is written with state code `internal_error` (RED), alerts
unchanged. The worker carries on at the next tick (architecture §8.1). `Cancelled` ends the worker.

### 6.5 What is read when, and the request budget

| Item | Requests | When |
|---|---|---|
| HXUI | `read("HXUI")`, compared with `printer.uid` (a mismatch is `problem = "wrong_printer"`) | the first cycle after a start, a paused tick or being unreachable, and with each slow set |
| PRIN, MSGNAM | `read` | every cycle |
| bag counter | `read(Company.bag_counter)` | every cycle, unless `bag_counter == "none"` |
| slow set: INKL, INKT, PHTP, DATE, TIME | `read` each | every `poll_slow_s` (60 s) |
| running message | `load_slot(0, deadline=...)` | with each slow set; and in the first cycle after a paused tick, after E's counter changed, after the printer became reachable, after `E.reread_token` changed (set `P.reread_seen` to it), and when MSGNAM differs from R's name (`read_running_now`); and in the next cycle after a Load failed or a confirming Load failed or wasn't made, until the message check has committed as failed (§6.4, owner decision 31 Oct; that retry isn't a required Load, so its failure keeps R) |
| daily item | one `read`, or `fonts(deadline=...)`/`firmware(deadline=...)` | one per cycle while a daily check is due (§6.10) |
| clock set | DATE, TIME, then at most one TD and DATE, TIME again (§6.11) | in a cycle of its own, once per daylight-saving change |
| second readings | 1 to 3 | only when the outcome would change (§3.8) |

Order in a cycle: HXUI, PRIN, MSGNAM, counter, the slow set, the daily item, the Load. A cycle that
does the daylight-saving clock set (§6.11) makes only HXUI, PRIN and the clock-set requests.

HXUI is read once a minute and after every start, pause or unreachable spell, not every cycle (§15
decision 35). INKT is checked against the company's ink list: a value that isn't the `name` of any
`Company.inks` entry is thrown away and asked once more at once. A second reply that is on the list is
used, and the first counts as one nonsense event (§6.8). A second reply equal to the first (the printer
consistently reports an ink Stripes doesn't know) isn't nonsense: `ink_name` keeps its previous value
and nothing is counted (the Send's machine check refuses such an ink anyway, send.md §10.5). A second
reply that differs from the first and isn't on the list counts one nonsense event, and `ink_name` keeps
its previous value.

**The message check** (C11b). The worker keeps, on its monotonic clock, `load_ok_mono` (the last
successful running-message Load, or the worker's start, §6.3) and `loads_failed` (running-message
Loads that failed in a row since the last success, the last paused tick or the last change of E's
counter; any of those resets it to 0, and a worker start carries it from P, §6.3). A Load fails when
it raises `WebReadFailed`, `NoReply`, `PrinterBusy` or `BadReply`. `reading.running_unchecked = loads_failed >=
UNCHECKED_LOADS or now − load_ok_mono >= UNCHECKED_SLOW_SETS × poll_slow_s`; it is computed only in
share mode (None in standalone, carrying the previous value). A failed Load is tried again at the
next cycle (§6.4). When the retry fails too (`loads_failed` 2; the retry isn't required, so R stays,
and only a failed required Load leaves R unknown), the message check counts as failed and the
`message_unreadable` warning starts, about 5 s after the first failure, and in any case within 3
minutes: amber `message_unchecked`, or a committed `drift` that stays red with the same alert and the
origin shown as unknown (second review, 31 Oct). A single failed Load followed by
a good one changes nothing. A successful Load sets `load_ok_mono =
now`, `loads_failed = 0` and `P.running_ok_at` to its wall time.

Failed confirmations (owner decision, 31 Oct). A first Load that succeeds but reads a different R
needs a second Load to agree (§3.8). Its success above (`loads_failed = 0`) must not hide a
confirmation that keeps failing (a web client sharing the printer's one reply buffer can make every
second Load fail), or the machine would stay green on the old R while every first Load reads another
origin. So the worker also keeps `confirms_failed`: confirming Loads that failed (or weren't made) in
a row, while a candidate waits for them. It is reset to 0 by a successful confirming Load (after that
second reading is judged, so the second reading agrees with the first about `running_unchecked`), by a
committed judgement whose R a successful Load of that cycle read, by a paused tick and by a change of
E's counter; a worker start or takeover starts it at 0. A first Load that reads a mismatch never
resets it. `reading.running_unchecked` is also True while `confirms_failed >= UNCHECKED_CONFIRMS`
(3). After a failed confirmation the next cycle loads again (§6.4), so a single failure delays a red
`drift` by one cycle (about 5 s), not a slow set. At the third failed confirmation in a row, when the
second reading commits nothing, the worker judges that reading again as a first reading: R is unknown
(the confirming Load failed) and `running_unchecked` is True, neither of which needs a second reading
(§3.8 steps 3 and 4, `evidence`), so amber `message_unchecked` with `message_unreadable` commits at
once, with the origin shown as unknown. Further Loads then follow the slow set again (the budget), and
two agreeing Loads still decide `drift`. With 3, 40 % of Loads failing at random gives red or amber
within about 20 s of the first Load after the change (§12.2 item 4).

Budget per printer at the default timings, steady state: 3 requests every 5 s (36 a minute), plus 1
HXUI, 5 slow reads and one Load (one Load request plus about one repeat) a minute: about 44 requests a
minute, 0.73 a second, under the plan's 1 a second. A failed Load adds one retry Load at the next cycle,
and at most `UNCHECKED_CONFIRMS` retries follow failed confirmations before the slow set's pace
returns, so a printer whose Loads always fail makes at most 2 Loads a minute. No cycle sends more
than 15 requests. Multi-value queries are never used (protocol.md refuses them).

### 6.6 Requests that fail, and backoff

| From a request | Effect on the reading |
|---|---|
| a reply (any value, busy, or a nonsense reply that the client threw away) | `failures = 0`; the item is None when it wasn't a good value |
| `NoReply` (after the client's own retry) | `failures += 1`; the rest of the cycle's requests are skipped |
| `PrinterBusy` | not a failure; the item is None |
| `BadReply` | not a failure; the item is None (the client counted the nonsense) |
| `WebReadFailed` | not a failure for reachability; `running` is None (with `running_reset` when a Load was required), and a running-message Load counts in the message check (§6.5) |
| `AuthFailed` | `problem = "login_failed"`; the rest of the cycle is skipped |
| `WrongPrinter` | `problem = "wrong_printer"`; the rest of the cycle is skipped |
| `ShareUnavailable` while checking the lock | abandon the cycle (no request without a lock check), §6.13 |

`reading.reachable = failures < timings.unreachable_after`. Because a failed request ends its cycle,
each cycle adds at most one failure: a printer that stops answering is judged unreachable at the third
failed cycle, about 15 s after it went quiet, and the second reading about a second later confirms it
(about 20 s). A dropout of a few seconds raises nothing.

Backoff: while P says unreachable, one attempt (`read("PRIN")`) is made `backoff_s[i]` after the end of
the previous one, with `i` = 0, 1, 2, 3, 3, … (5, 10, 30, 60, 60 … s). When an attempt gets a reply,
`failures = 0`, the reading is reachable, and the second reading confirms it; the next cycles are
normal and start with HXUI and a Load.

### 6.7 Counting bags

1. The worker keeps a list of `(monotonic, count)` samples of the bag counter, newest last.
2. A count lower than the previous sample is a reset (expected after a message change on some
   counters): the list restarts from the new count, adding no bags.
3. A rise greater than `MAX_BAGS_PER_S × seconds since the previous sample + 10` doesn't make sense: the
   reading is thrown away (the item is None) and counts as one nonsense event (§6.8). protocol.md's
   reply check covers the format; this is the "counters only rise by believable amounts" check.
4. `bags_recent` = the bags added by samples within the last `silent_window_s`.
5. A new worker hasn't seen bags yet, so for its first `silent_window_s` it reports
   `max(own count, prev.bags_recent or 0)`, erring towards "was printing".
6. `bag_counter == "none"`: no counter is read; `bag_count` and `bags_recent` stay None, and
   `was_printing` relies on printing alone.

### 6.8 Something else talking to the printer

1. After each request the worker notes how much `client.nonsense` and `client.busy_replies` went up
   (protocol.md §13), each unit as one event with its monotonic time, plus the bag-counter events of
   §6.7 and the ink-name events of §6.5.
2. `reading.foreign_client` is True when at least `FOREIGN_MIN` (3) events happened within the last
   `FOREIGN_WINDOW_S` (600 s), and stays True until `FOREIGN_CLEAR_S` (1800 s) pass without any.
3. Not computed in standalone mode, where several PCs may legitimately overlap (it stays False).
4. The warning is `foreign_client`: "Something else is talking to printer {printer}", to
   administrators and the monitor, without sound.

### 6.9 The printer clock

With each slow set: `date = read("DATE")`, `time_ = read("TIME")`, then `t = time_source.cached()`
(never a query: the `hx-time` thread keeps the cache fresh, §6.2 item 6; a worker never waits on the
time server). `printer = protocol.clock_from(date, time_)`; `offset = (printer -
t.local).total_seconds()`. If `abs(offset) > 43_200`, it is a pair
read across midnight: add or subtract 86,400 to bring it nearest 0. If either read failed,
`clock_offset_s` is None for this cycle (carried). The warning `clock_off` uses `clock_warn_s` (120 s).
When `t.source == "pc"`, the offset is against this PC's clock; the warning says so in a detail
("compared with this PC's clock").

### 6.10 Daily checks

1. Due when `P.daily` is empty, or `reading.at - P.daily["at"] >= DAILY_S` (or below −300: a clock set
   back), or `P.daily["machine_revision"] != machine.revision` (an administrator changed the machine,
   for example its encoder values).
2. Items, one per cycle, in order: `ECOMOD`, `PLDEL`, `MNPL`, `PRPL`, `PLVO`, `PLWR`, `NZRO`; on encoder
   machines `ENWHD`, `ENPPR`, `ENTPM`; then `fonts()` and `firmware()`. A failed item keeps its previous
   value and is listed in `errors`.
3. When the last item is done, the reading carries `daily`:

   ```json
   {"at": 1793275200.0, "by": "FLOOR01_EXAMPLE_jsmith_4242", "machine_revision": 3,
    "values": {"ECOMOD": 0, "PLDEL": 1210, "MNPL": 450, "PRPL": 220, "PLVO": 9500, "PLWR": 27,
               "NZRO": 2, "ENWHD": 56, "ENPPR": 5000, "ENTPM": 1},
    "encoder_ref": {"ENWHD": 56, "ENPPR": 5000},
    "fonts": ["CourierN", "Arial", "ArialNarrow", "ArialNarrowB", "TimesNewRom", "LinearBeam"],
    "firmware": "69.75.10", "errors": []}
   ```

4. Log `daily_check` (§9) the first time and whenever a value, the font list or the firmware differs
   from the previous check, with `changed: {name: [old, new]}`.
5. Encoder reference (C24): the machine record's `encoder_wheel_mm` (rounded) and `encoder_ppr` when set;
   for a value the record doesn't have, `encoder_ref` from the first daily check that read it (kept in
   `daily` from then on). C24 is true while `ENWHD` or `ENPPR` differs from its reference. It ends when
   the printer is set back, or an administrator records the new values on the machine (which also makes
   the check due at once, rule 1).
6. C25 (`fonts_differ`) compares `fonts` with `Company.standard_fonts`. A missing list (an error) raises
   nothing here: the Send's own machine check refuses to send (protocol.md §10.8).
7. A firmware change is logged only.

### 6.11 The daylight-saving clock set

The poller's one write (plan; architecture §7.3 rule 6). This section is the only specification of it;
send.md §9.6 points here. `hx_poller` can't import `hx_send`, so it runs the clock-set steps of send.md
§9.5 itself, with the same functions: `hx_message.clock_command`, `hx_protocol.clock_from` and
`hx_message.CLOCK_SET_TOLERANCE_S`.

1. **The current change.** Every heartbeat the coordinator calls `rc = hx_time.recent_dst_change(
   clock.time(), within_s=DST_KEEP_S, offset_fn=time.offset_fn)` (send.md §9.3 item 5). None: there is
   no pending change. Otherwise `rc = (old_s, new_s, at)`, with `at` the UTC second the offset changed,
   and the change id is `dst_change_id(at, new_s, offset_fn=time.offset_fn)` = `f"{to_local(at,
   offset_fn=...):%Y%m%d}{'+' if new_s >= 0 else '-'}{abs(int(new_s))}"`, for example `20261101-18000`.
   Every PC computes the same id from the same time-zone rules, whether it was running at the change
   or started up to `DST_KEEP_S` (a day) later, so a change while every PC was off (2 AM on a Sunday) is
   still caught up once the first PC starts. The offset function is the `TimeSource`'s, so a test that
   injects one controls the change.
2. **When.** Each share-mode worker, while there is a pending change: when the printer is trusted (its
   last committed facts) and `P.dst.get("change") != id`, the next cycle is a clock-set cycle: HXUI and
   PRIN as usual, then the steps below instead of the other items. Every request follows §6.4 step 7
   (the lock checked, P kept fresh, the call with its deadline), so the TD is sent right after its own
   lock check. A fresh lock abandons the cycle (before the TD, item 3.6 puts `dst` back); the next
   free cycle starts the steps again.
3. **Steps.** `t = time_source.cached()` first (never a query, §6.9).
   1. `d0 = read("DATE")`, `t0 = read("TIME")`; `before = clock_from(d0, t0)`; `expected =
      time_source.now_from(t).local`; `diff = (before − expected).total_seconds()`, brought to the
      nearest whole day as in §6.9.
   2. `abs(diff) <= clock_warn_s`: the clock is already right (a Send or Set clocks set it since the
      change). No TD. Record `ok` True, `skipped` True, `sent` False.
   3. **Record first.** Before the TD, write P with `dst = {"change": id, "at": <wall time>, "ok":
      False, "skipped": False, "sent": True, "by": <heartbeat key>, "source": t.source}` (counter + 1,
      everything else as last written). If that write fails, send no TD and end the cycle (the steps
      run again at the next cycle). Every later P write of the cycle, including §6.4 step 7.2's
      keep-fresh writes, carries this `dst`. So once a TD may have gone out, P already says so, and a
      poller that takes over after a crash, a failed write or a §6.13 stop never sends a second one
      (the same rule as send.md decision 38 for PRINTMSG).
   4. Then `client.command(hx_message.clock_command(time_source.now_from(t).local))`, once, by §6.4
      step 7's rules (its own lock check right before it).
   5. `d1 = read("DATE")`, `t1 = read("TIME")`, `after = clock_from(d1, t1)`, `expected =
      time_source.now_from(t).local`. `abs(after − expected) <= CLOCK_SET_TOLERANCE_S` (90 s): `ok`
      True. Otherwise read DATE and TIME once more (a pair read across midnight can be a day out) and
      check again; still off: `ok` False with the message "{machine}'s clock reads {printer_time} after
      it was set to {expected}." (send.md's text).
   6. A read that fails in step 1 ends the cycle with nothing recorded (no TD was sent, so the steps run
      again at the next cycle). When the cycle ends after step 3's write but before the TD request was
      sent (a fresh lock at its lock check, the time bound of §6.4 step 7.2, the cancel token), the
      worker knows no TD went out: its next P write puts `dst` back to what it was before step 3, so
      the steps run again at the next free cycle; if that write fails, the record stands and this
      change gets no automatic TD (the safe side; the `clock_off` warning and the next Send cover it).
      Once the TD request has been sent, whatever happens next, the TD is never sent again for this
      change (plan: "never repeated"): a `NoReply`, `CommandRefused` or failed read-back is recorded
      with `ok` False.
4. **Record.** `P.dst = {"change": id, "at": <wall time>, "ok": bool, "skipped": bool, "sent": bool,
   "by": <heartbeat key>, "source": t.source}`, written with the cycle's P; after a TD it is step 3's
   record with `ok` overwritten by the result. Because it is in P, a poller that takes over the printer
   later doesn't do it again for the same change, and a printer that was off at the change gets its
   clock set when it next answers within `DST_KEEP_S`; after that the `clock_off` warning and the next
   Send cover it. A record left with `sent` True and `ok` False by a poller that stopped before item 4
   is never retried, for the same reason.
5. **Log** `dst_clock_set` to `hx-events` (§9; plan: "logged as automatic") with exactly these fields:
   `change`, `change_at` (`at` of item 1), `previous_offset_s`, `offset_s` (`old_s` and `new_s` of item
   1, the PC's UTC offsets before and after the change; not the `offset_s` inside `clock`), `ok`,
   `skipped`, `msg` (Msg or null), and `clock`, send.md §9.5's record: `{"sent": <the TD's date and
   time "%Y-%m-%d %H:%M"> or None when skipped, "before": before "%Y-%m-%d %H:%M:%S", "after": after or
   None, "difference_s": <after (or before, when skipped) minus expected, rounded to 0.1>, "source":
   t.source, "server": t.server, "offset_s": round(t.offset_s, 3)}`.
6. Not done in standalone mode. When `t.source == "pc"` (the time server hasn't answered the `hx-time`
   thread), the PC's time is used, as the plan allows, and the log says `source "pc"`.

```python
def dst_change_id(at: float, new_offset_s: float, *, offset_fn) -> str   # item 1; pure
```

### 6.12 Standalone mode

The share is down. Polling carries on only on PCs with the MapleJet monitor or the floor board open
(plan).

1. The coordinator stops every share-mode worker as soon as the share is unavailable (or §6.13
   suspended share-mode polling); no `yielded_to` can be written. It enters standalone mode only when
   `set_monitor_open` said a monitor or the floor board is open **and** the share has been unavailable
   without a break for at least `lock_stale_s` (60 s, on its monotonic clock; any successful share
   operation restarts the count). Until then the mode is `idle`. Other PCs that still reach the share
   take this PC's printers over within 15 to 20 s, and any lock this PC can't see is stale for them
   after 60 s, so a short outage never makes this PC poll alongside them (architecture §7.3 rule 7
   names the residual risk of a longer partial outage). On entering it draws a random offset in
   `[0, standalone_poll_s)`.
2. Every `standalone_poll_s` (30 s) from that offset, and on `refresh_now()`, it checks every printer of
   the last good `HxData` on per-printer worker threads: the quick items only (HXUI, PRIN, MSGNAM, the
   bag counter), one request at a time per printer, skipping any printer in `store.local_locks` (this
   PC's own Sends still pause it). No slow set, no Load, no daily item, no clock set. HXUI is read at
   every standalone check because the cached data can't be refreshed meanwhile.
3. Judging uses the same `judge` with `reading.standalone = True`, the last E read from the share (the
   watcher's and the coordinator's cache), L = None, and prev = this PC's previous local status. At the
   first check prev is None, except for a known origin mismatch (owner decision, 31 Oct): when the
   newest P this PC read from the share (the coordinator's last read, `seen_polls()`, or what this PC's
   own worker last wrote, whichever has the higher counter) has `expected_counter` equal to the cached
   E's counter and either the code `drift` or an active `drift` alert with R not matching E (R None
   included: a failed Load left it unknown, C11e), or a message share mode couldn't check (the code
   `message_unchecked` or `running_unchecked` True; second review, 31 Oct), prev is that P (its
   committed state, R, and open alerts with their acks) with `standalone` True, this PC as `poller`,
   and `running_unchecked` True for a message that couldn't be checked. Only those are carried, so no
   other state becomes sticky. C3, C4, C11 and C11b are skipped, C11c keeps a known drift (red, its
   alert open and sounding unless acknowledged, R's origin; at best amber `unknown_while_down` once
   MSGNAM names another message or when R is unknown), C11d keeps a message that couldn't be checked
   at best amber `unknown_while_down` with the origin unknown, and C12 gives `unknown_while_down`
   (§3.4). Standalone can't clear either; only share mode's own Loads and second reading can. A PC that never read P for the machine (it started while the share was
   already down) has nothing to carry: prev is None, as before. There are still no Loads, and one
   request at a time per printer. Second readings apply as usual (1 s later).
4. Results stay in memory: `local_statuses()` returns a `PollStatus` per machine, with `poller` = this
   PC's key, `counter` + 1 per check, and `standalone` True. Nothing is written to the share; log entries
   go through `DataService.append_log`, which queues them on this PC, with `"standalone": true`.
5. `refresh_now()` returns False (and does nothing) when it was used less than `refresh_now_min_s`
   (10 s) ago on this PC, or when not in standalone mode.
6. `next_check_s` (in `PollerUpdate`) is the time left to the next check.
7. When the share is back, the coordinator leaves standalone mode, drops the local statuses, and starts
   share mode from §6.2 (every start follows §6.3, settle included). A worker started by the first share
   pass (still in standalone mode) gets this PC's local status of its machine, so its start write shows
   a RED or AMBER standalone finding instead of `checking` (§6.3 step 1). A share P that holds a
   standalone `unknown_while_down` shows the origin as unknown (§5.3 rule 0). A carried drift ends only
   there: the share P still holds the drift with the same alert id, and the worker's first Load and its
   second reading decide (owner decision, 31 Oct). That first Load is a required one (a worker start):
   when it fails, R is unknown, and C11e keeps the drift alert (the same id, acks kept, red, the origin
   unknown) until a Load that works and its second reading decide; a failed Load never ends it (second
   review, 31 Oct).

### 6.13 Share failures

1. A worker whose P writes or lock reads have failed for `stale_s / 2` (7.5 s, on its monotonic clock)
   stops share-mode polling of that printer (storage.md §12.6 rule 7) and tells the coordinator, which
   suspends share mode until `store.available` has been True again for `heartbeat_s` and a P write
   succeeds. A lock read or P write that took longer than `stale_s / 4` (3.75 s) counts as failed for
   this rule even when it succeeded, and §6.4 step 7 stops the cycle's requests as soon as the last
   good P write is `stale_s / 2` old.
2. A worker never makes a printer request without a successful lock check in the same cycle.
3. While suspended, the coordinator goes to standalone or idle mode (§6.12).

### 6.14 What goes in poll.json

The fields are architecture §5.8's plus §11.2's. "Committed" fields change only through `judge`'s
commit (§3.8).

| Field | Value |
|---|---|
| `machine_id`, `printer_id` | the machine and its printer |
| `poller`, `written_at` | set by `ShareStore.write_poll` |
| `counter` | + 1 at every tick |
| `paused_for_lock` | the lock token while paused (`"local"` for a local lock without a file), else `""` |
| `yielded_to` | `""`, or the heartbeat key when handing over |
| `reachable`, `problem`, `printing`, `running`, `ink_level`, `clock_offset_s`, `daily`, `foreign_client`, `running_unchecked` | committed facts |
| `running_ok_at` | wall time of the last successful running-message Load (§6.5); display only |
| `failures`, `last_answer_at`, `message_name`, `bag_count`, `bags_recent`, `ink_name`, `head_temp_c` | the latest values |
| `state` | the committed `MachineSnapshot` |
| `alerts` | the active alerts |
| `expected_counter` | `E.counter` at the last judgement (0 when there was no E) |
| `reread_seen` | the last `E.reread_token` acted on |
| `unreachable_since` | wall time when `reachable` last became False; None while reachable |
| `dst` | §6.11 item 4: `{"change", "at", "ok", "skipped", "sent", "by", "source"}` of the current change (written before the TD, §6.11 item 3.3), `{}` before the first |
| `standalone` | True only in `local_statuses()` |

Example (a machine in drift, alert unacknowledged):

```json
{
 "format_version": 1,
 "file_type": "poll",
 "machine_id": "mc_5d1e2f3a4b",
 "printer_id": "pr_9c8b7a6d5e",
 "poller": "FLOOR01_EXAMPLE_jsmith_4242",
 "counter": 18741,
 "written_at": 1793275240.2,
 "paused_for_lock": "",
 "yielded_to": "",
 "reachable": true,
 "failures": 0,
 "last_answer_at": 1793275240.1,
 "printing": true,
 "message_name": "(message name, send.md format)",
 "bag_count": 436530,
 "bags_recent": 121,
 "ink_name": "Example Black Plastic",
 "ink_level": 1,
 "head_temp_c": 26,
 "clock_offset_s": 1.4,
 "running": {"slot": 9, "name": "(message name, send.md format)",
             "fingerprint": "0a1b...(64 hex)", "origin_line": "Product of USA",
             "read_at": 1793275238.9, "preview_sha256": "85f0...(64 hex)"},
 "daily": {},
 "state": {"code": "drift", "colour": "red",
           "headline": {"text": "Not printing what Stripes sent", "values": {}},
           "since": 1793275239.4,
           "detail": [{"text": "Message {name} in slot {slot}.",
                       "values": {"name": "(message name)", "slot": 9}}]},
 "alerts": [{"id": "mc_5d1e2f3a4b.drift.1793275239400", "machine_id": "mc_5d1e2f3a4b",
             "line_id": "ln_1a2b3c4d5e", "kind": "drift", "sound": true,
             "msg": {"text": "{machine} isn't printing what Stripes sent",
                     "values": {"machine": "Line 1 Bagger 1"}},
             "started_at": 1793275239.4,
             "recipients": ["user:EXAMPLE\\akim", "user:EXAMPLE\\jsmith", "floor"],
             "active": true, "colour": "red", "ref": "", "acked_by": "", "acked_at": null,
             "ended_at": null, "waiting": []}],
 "foreign_client": false,
 "problem": "",
 "expected_counter": 42,
 "reread_seen": "",
 "unreachable_since": null,
 "running_ok_at": 1793275238.9,
 "running_unchecked": false,
 "dst": {},
 "standalone": false
}
```

### 6.15 Alert bookkeeping in the coordinator

Every `heartbeat_s`, for the alerts in the latest P of each printer this PC polls:

1. The waiting rule of §4.6, using the `live` heartbeats from §6.2. The worker writes the updated
   `waiting` list at its next tick (the coordinator hands it over in memory; P has one writer).
2. Nothing else: starts, ends and acknowledgements are logged by the worker (§6.4 step 12).

## 7. The status watcher

`StatusWatcher` (architecture §6.8) runs `hx-watch`. Every `watch_s` (5 s), or at once after
`refresh_now()` or `acknowledge()`:

1. `data = DataService.current`. None: post `BoardUpdate(BoardView(lines=[], share_ok=store.available,
   generated_at=clock.time()))` and wait.
2. **Share mode** (`store.available`): for every active machine of an active line,
   - P: from `poller.seen_polls()` when the coordinator read it within the last `heartbeat_s`, else
     `store.read_poll(machine_id)`. `FormatTooNew` gives a placeholder `PollStatus(machine_id=...,
     printer_id=..., poller="", state=MachineSnapshot(code="update_stripes", colour=GREY,
     headline=Msg(STATE_TEXTS["update_stripes"]), since=0.0))`;
   - E: `store.read_expected(machine_id)`; `FormatTooNew` marks the machine `update_stripes` the same
     way;
   - L: `store.read_lock(printer_id)`;
   - then `read_line_status` for every active line, and `poll_ages[mid] = store.poll_age(mid)`,
     `lock_ages[pid] = store.lock_age(pid)`.
   - acks: `store.read_ack(id)` for every active alert without `acked_by` (found acks are cached by the
     store), merged with this PC's own acks.
   - A `ShareUnavailable` part-way through: use the last good value of everything not yet read this
     cycle and build the board with `share_ok=False`.
3. **Standalone** (`poller.mode == "standalone"`): P = `poller.local_statuses()`, ages from the watcher's
   own `CounterWatch` over their counters, E and line statuses = the last good ones, L =
   `store.local_lock_info(printer_id)` for printers in `store.local_locks`, with `lock_ages[printer_id]
   = 0.0`, and None otherwise, acks = the acks this watcher read from the share before the outage plus
   this PC's own (a carried alert never rings again for an acknowledged occurrence). A machine with no
   local status yet (before this PC's first standalone check of it) shows the last good P from the
   share, its age still growing, as in item 4, so a red machine keeps its colour, alerts, sound and
   origin over the change of mode instead of showing "Checking..." (owner decision, 31 Oct). A machine
   whose P this watcher never read shows "Checking..." and then NOT MONITORED, as before.
4. **Share down, poller idle**: the last good P, E and line statuses, with ages still growing (so every
   machine becomes `not_monitored` after 30 s), `share_ok=False`. The last good P is the newer (by
   counter) of the watcher's last read and what this PC's own worker last wrote, so a drift this PC
   committed just before the outage shows red before any pass read it (owner decision, 31 Oct).
5. `board = board_view(...)`; set `board.next_check_s` from the last `PollerUpdate`; set `latest`; post
   `BoardUpdate(board)`. `latest` and its `BoardUpdate` change together under the watcher's lock, with
   every own ack made during the pass applied first, so an `acknowledge()` while a pass reads the share
   is never undone by that pass's board, and a pass that ends during `acknowledge()` is never replaced
   by the older board (§4.5 item 2). The lock is never held across share I/O.
6. Share mode, a P this watcher has not yet seen move whose `written_at` is `not_monitored_s` or more
   old by this PC's clock while `poll_age` is still below `not_monitored_s` (storage.md §10 rule 1
   counts a counter seen for the first time as fresh): its age is given as None, so it shows
   `checking`, not last night's state as just checked, until it moves or becomes `not_monitored`
   (owner-approved divergence, 31 Oct). Once seen moving, `written_at` is never used again, so a PC
   whose clock is off never loses a P that is being written.

`acknowledge(alert_ids: list[str]) -> None` (§11.3): records `Ack`s in memory at once, wakes the loop
(the next board applies them), and queues the share writes, which the watcher thread makes after
building that board. A failed write is retried every `watch_s` for `ACK_RETRY_S`. In standalone mode
nothing is written (§4.5).

## 8. Floor board and monitor cards

### 8.1 The floor board

The production-schedule PC runs Stripes with the MapleJet monitor in floor-board mode (`FloorBoard`,
ui.md). Content and order come from `floor_board_rows(board)` and `board_banner(board)`; ui.md draws
them.

Layout (top to bottom; ui.md §10.3 has the drawing rules, sizes and examples, and is the one to
follow where this summary is shorter):

1. A header line: "MapleJet floor board", the time, and `board_banner(board)` when it isn't None
   (with Refresh now in standalone mode, enabled per `refresh_now_min_s`).
2. The machines in `floor_board_rows` order, grouped under a band per line with the line's name (half
   a row tall). Every row has the same height, filling the screen (at most a quarter of it each, never
   under 44 px); rows that can't all fit scroll, with a scrollbar shown only then. Each row, left to
   right, in fixed columns:

   | Part | Content | Share of the row width |
   |---|---|---|
   | colour bar | `FloorRow.colour` (`COLOUR_HEX`), full height; flashes (on and off every `FLOOR_FLASH_MS`) while the machine has `unacked_sound` | 2.5 % |
   | name | the machine's name without its line's name in front (`short_name`, ui.md §10.3) | 14 % |
   | origin | `origin_line` in the bag font, every origin on the board at one size (the largest at which the longest fits), never shortened; or "(origin unknown)" | 44 % |
   | status | `text(status)` in the row's colour | 27 % |
   | age | `checked_text(checked_age_s)`; "NOT MONITORED" rows are drawn desaturated (grey text) | 12.5 % |

   Nothing wraps: a name, status or age too long for its column is drawn smaller, then ends in "…".
3. Rows of one line sit together under their band, with a thin divider between rows; a heavier rule
   divides the lines. When a line's origins differ, each of its rows still shows its own origin, so
   every origin is listed.
4. The floor-board PC rings for alerts with the `"floor"` recipient (`sounding_alerts(...,
   floor_board=True)`). Its Acknowledge button appears only when the signed-in user may acknowledge
   (§4.5).

The three per-user settings are `HxSettings` fields (architecture §6.10; `maplejet.json`, storage.md
§7.1):

| Setting | Field | Values | Default |
|---|---|---|---|
| Open the floor board at startup | `floor_board_at_startup` | bool | False |
| Which connected screen | `floor_board_screen` | index into `hx_win.monitors()` (0 = primary); a missing screen falls back to 0 | 0 |
| Size | `floor_board_size` | `"full"` (full screen, no title bar, on that screen) or `"<width>x<height>"` (a normal window of that size at the screen's top-left), e.g. `"1280x720"` | `"full"` |

`hx_ui.startup` opens it when `floor_board_at_startup` is set (architecture §10.1). An open floor board
calls `Poller.set_monitor_open(True, floor_board=True)`, so the PC polls in standalone mode and writes
`floor_board` in its heartbeat.

### 8.2 Monitor cards

`LineBoard` (in the MapleJet tab and the MapleJet monitor) shows a card per line with its machines,
from `BoardView`:

1. Line card header: `LineView.name`, `LineView.headline`, the colour, and `checked_text` of the line's
   oldest age. NOT MONITORED (no machine monitored) greys the whole card out and shows "NOT MONITORED"
   as its headline.
2. Machine rows: name, the colour, the headline, the origin (`BagText`), `checked_text(checked_age_s)`,
   the details, and the active alerts (each with who acknowledged it, or "Waiting for {user} to sign
   in.").
3. Buttons from the view's flags (§5.3): Stop (`can_stop`), Resume (`can_resume`), Send (`needs_send`
   makes it the main button), Confirm (`confirm_pending_by`, enabled for that user only), Acknowledge
   (any active alert without `acked_by`, for users who may acknowledge). Set clocks is on the line card
   for senders. BLUE machines show no buttons.
4. Grey `off` machines show "Printer off" in grey with no alert, as the plan wants ("Machines are often
   just switched off").
5. Order: lines by name, machines by name (as the floor board).

## 9. Event log entries

All go to `hx-events` through `DataService.append_log` (storage.md §6, §13.6). Each entry has `type`,
`machine_id` (and `printer_id`, `line_id` where known) and `poller` (the heartbeat key). `Msg`s are
stored with `Msg.to_json()`.

| type | When | Extra fields |
|---|---|---|
| `state_changed` | a committed code change | `from_code`, `to_code`, `colour`, `headline`, `detail`, `standalone` |
| `alert_started` | an alert starts | `alert` (the whole `Alert`) |
| `alert_ended` | an alert ends | `alert_id`, `kind`, `started_at`, `ended_at`, `acked_by` |
| `alert_acknowledged` | the poller first sees an ack | `alert_id`, `kind`, `by`, `computer`, `at` |
| `alert_waiting` | a recipient has no live heartbeat (§4.6) | `alert_id`, `kind`, `user` |
| `alert_reached` | that recipient signs in | `alert_id`, `kind`, `user`, `computer` |
| `polling_started` | a worker starts (§6.3) | `reason`, `counter` |
| `polling_stopped` | a worker stops | `reason` (`handover`, `rank`, `stop`, `share`), `yielded_to` |
| `daily_check` | §6.10 rule 4 | `daily`, `changed` |
| `dst_clock_set` | §6.11 | `change`, `change_at`, `previous_offset_s`, `offset_s`, `ok`, `skipped`, `msg` (Msg or null), `clock` (send.md §9.5's record, §6.11 item 5) |
| `poller_mode` | the coordinator's mode changes | `from_mode`, `to_mode` |
| `internal_error` | §6.4 | `error` (one line), `traceback` (text) |

None of these entries has a `send_id`, `run_id`, `action_id`, `record_id` or `film_id` at the top
level, so the `ref` part of each machine entry's log name is the machine id (storage.md §6.3), and
`list_logs(..., ref=machine_id)` finds a machine's history. `poller_mode` has no machine (ref `x`).

## 10. Texts and Spanish

`hx_state` defines `STATE_TEXTS`, the alert texts and `ES`, and calls `i18n.add_spanish(ES)`. Texts
already in `i18n.ES` ("Printing", "Checking...") are listed with their existing Spanish; `add_spanish`
accepts identical pairs. "Off" is already "Nunca" in `i18n.ES` (an auto-refresh choice), so the grey
state says "Printer off". ui.md must not register different Spanish for any English text here; if it
needs the same words, it uses these.

### 10.1 State headlines

| Code | English | Spanish |
|---|---|---|
| `send_in_progress` | Sending: {user} on {computer} | Enviando: {user} en {computer} |
| `switch_pending` | Switching to the new message: {user} on {computer} | Cambiando al mensaje nuevo: {user} en {computer} |
| `test_running` | TEST by {user}, step {step} of {of} | PRUEBA de {user}, paso {step} de {of} |
| `test_running` (no step yet) | TEST by {user} | PRUEBA de {user} |
| `action_in_progress` (stop) | Stopping printing: {user} | Deteniendo la impresión: {user} |
| `action_in_progress` (resume) | Resuming printing: {user} | Reanudando la impresión: {user} |
| `action_in_progress` (clock) | Setting the clock: {user} | Ajustando el reloj: {user} |
| `action_in_progress` (other) | Printer in use: {user} on {computer} | Impresora en uso: {user} en {computer} |
| `checking` | Checking... | Revisando... |
| `not_monitored` | NOT MONITORED | SIN MONITOREO |
| `update_stripes` | Update Stripes to see this machine | Actualice Stripes para ver esta máquina |
| `not_set_up` | No printer set up | No hay una impresora configurada |
| `internal_error` | Stripes hit a problem checking this machine | Stripes tuvo un problema al revisar esta máquina |
| `wrong_printer` | Wrong printer at this address | Impresora equivocada en esta dirección |
| `login_failed` | Stripes can't log in to this printer | Stripes no puede iniciar sesión en esta impresora |
| `test_orphaned` | Test message still printing | El mensaje de prueba sigue imprimiendo |
| `test_interrupted` | Test stopped on {computer}: needs a Send | Prueba detenida en {computer}: necesita un envío |
| `interrupted_unknown` | Send interrupted on {computer}: check the bags now | Envío interrumpido en {computer}: revise las bolsas ahora |
| `interrupted_old` | Send interrupted on {computer} | Envío interrumpido en {computer} |
| `interrupted_new` | Send interrupted, not confirmed | Envío interrumpido, no confirmado |
| `stop_failed` | Still printing: stop it at the printer now | Sigue imprimiendo: deténgala en la impresora ahora |
| `stop_requested` | Stop it at the printer: press Print On/Off | Deténgala en la impresora: presione Print On/Off |
| `failed_after_switch` | Send failed after the switch: check the bags now | El envío falló después del cambio: revise las bolsas ahora |
| `failed_before_switch` | Send failed: still printing the previous message | El envío falló: sigue imprimiendo el mensaje anterior |
| `left_red` | Left red by {user} | Dejada en rojo por {user} |
| `rejected` | Rejected by {user} | Rechazado por {user} |
| `printing_on_unexpected` | Printing was switched on at the printer | Se encendió la impresión en la impresora |
| `unreachable_printing` | Not answering while printing bags | No responde y estaba imprimiendo bolsas |
| `printing_off` | Printing was switched off at the printer | Se apagó la impresión en la impresora |
| `drift` | Not printing what Stripes sent | No imprime lo que envió Stripes |
| `unknown_while_down` | Can't check the message while the server is down | No se puede revisar el mensaje mientras el servidor no responde |
| `ink_out` | Out of ink | Sin tinta |
| `cartridge_error` | Cartridge not detected | Cartucho no detectado |
| `stopped_needs_send`, `test_finished` | Stopped, needs a Send | Detenida, necesita un envío |
| `off` | Printer off | Impresora apagada |
| `not_printing` | Printing is off | La impresión está apagada |
| `message_unchecked` | Can't read the message on this printer | No se puede leer el mensaje de esta impresora |
| `unconfirmed` | Send not confirmed | Envío no confirmado |
| `confirmed_with_notes` | Printing, not fully checked | Imprimiendo, sin revisión completa |
| `not_from_stripes` | Not sent from Stripes yet | Todavía no se envió desde Stripes |
| `stopped` | Stopped by {user} | Detenida por {user} |
| `idle` | Not printing | No está imprimiendo |
| `running_confirmed` | Printing | Imprimiendo |

### 10.2 Alert messages (`Alert.msg`)

Values: `machine` = `Machine.name`, `printer` = `Printer.name`, `computer`, `user`, `minutes`.

| Kind | English | Spanish |
|---|---|---|
| `send_failed` | Send failed on {machine} | El envío falló en {machine} |
| `send_rejected` | {user} said something's wrong on {machine} | {user} indicó que algo está mal en {machine} |
| `send_interrupted` | Send interrupted on {machine} ({computer}) | Envío interrumpido en {machine} ({computer}) |
| `stop_failed` | Stop failed on {machine}: stop it at the printer | No se pudo detener {machine}: deténgala en la impresora |
| `drift` | {machine} isn't printing what Stripes sent | {machine} no imprime lo que envió Stripes |
| `printing_off` | Printing was switched off on {machine} | Se apagó la impresión en {machine} |
| `printing_on` | Printing was switched on at the printer on {machine} | Se encendió la impresión en la impresora de {machine} |
| `ink_out` | {machine} is out of ink | {machine} se quedó sin tinta |
| `cartridge_error` | Cartridge not detected on {machine} | Cartucho no detectado en {machine} |
| `went_silent` | {machine} stopped answering while printing bags | {machine} dejó de responder mientras imprimía bolsas |
| `test_still_printing` | Test message still printing on {machine} | El mensaje de prueba sigue imprimiendo en {machine} |
| `encoder_changed` | Encoder settings changed on {machine} | Cambió la configuración del encoder en {machine} |
| `low_ink` (INKL 2) | Low ink on {machine} | Poca tinta en {machine} |
| `low_ink` (INKL 3) | Very low ink on {machine} | Muy poca tinta en {machine} |
| `low_ink` (INKL 4, not printing) | No ink on {machine} | Sin tinta en {machine} |
| `low_ink` (INKL 0, not printing) | No cartridge detected on {machine} | No se detecta cartucho en {machine} |
| `clock_off` | Printer clock on {machine} is {minutes} min off | El reloj de la impresora de {machine} tiene {minutes} min de diferencia |
| `foreign_client` | Something else is talking to printer {printer} | Algo más se está comunicando con la impresora {printer} |
| `setup_problem` (wrong printer) | Another printer answers at the address of {machine} | Otra impresora responde en la dirección de {machine} |
| `setup_problem` (login) | Stripes can't log in to the printer of {machine} | Stripes no puede iniciar sesión en la impresora de {machine} |
| `fonts_differ` | Printer {printer} doesn't have the standard fonts | La impresora {printer} no tiene las fuentes estándar |
| `message_unreadable` | Stripes can't read the message on {machine} | Stripes no puede leer el mensaje de {machine} |
| `not_from_stripes` | {machine} is printing a message that wasn't sent from Stripes | {machine} está imprimiendo un mensaje que no se envió desde Stripes |

`clock_off` rounds `minutes` to the nearest whole minute, at least 2.

### 10.3 Details, lines and the board

| English | Spanish |
|---|---|
| Started by {user}. | Iniciado por {user}. |
| Started by {user}. This machine needs a new Send. | Iniciado por {user}. Esta máquina necesita un nuevo envío. |
| After a test by {user}. | Después de una prueba de {user}. |
| Confirmed by {user}. | Confirmado por {user}. |
| Waiting for {user} to confirm. | Esperando la confirmación de {user}. |
| Message last read {minutes} min ago. | Mensaje leído por última vez hace {minutes} min. |
| Stripes hasn't been able to read the message yet. | Stripes todavía no ha podido leer el mensaje. |
| Not answering for {minutes} min. | No responde desde hace {minutes} min. |
| A Stop on {computer} didn't finish. | Una detención en {computer} no terminó. |
| A Resume on {computer} didn't finish. | Una reanudación en {computer} no terminó. |
| A clock set on {computer} didn't finish. | Un ajuste de reloj en {computer} no terminó. |
| Message {name} in slot {slot}. | Mensaje {name} en la posición {slot}. |
| Compared with this PC's clock. | Comparado con el reloj de esta PC. |
| Waiting for {user} to sign in. | Esperando a que {user} inicie sesión. |
| No machines | Sin máquinas |
| {machine}: {state} | {machine}: {state} |
| Different origins on this line | Orígenes distintos en esta línea |
| Not checked yet | Todavía sin revisar |
| checked {seconds} s ago | revisada hace {seconds} s |
| checked {minutes} min ago | revisada hace {minutes} min |
| Server unreachable: showing this PC's own checks · next check in {seconds} s | Servidor inaccesible: se muestran las revisiones de esta PC · próxima revisión en {seconds} s |
| Server unreachable: this PC isn't checking any printers. | Servidor inaccesible: esta PC no está revisando ninguna impresora. |

## 11. Interfaces added here

### 11.1 hx_state

Dataclass conventions are hx_model's (architecture §5): `= []` and `= {}` mean default factories.

```python
STATE_CODES: dict[str, str]           # code -> colour, exactly the §2 tables (40 codes)
STATE_TEXTS: dict[str, str]           # key -> N_ headline (§10.1): one key per code, except that
                                      #   action_in_progress has "action_in_progress:stop", ":resume",
                                      #   ":clock" and ":other" instead, and "test_running:nostep" is an
                                      #   extra key
PRECEDENCE: tuple[str, ...]           # §3.5
BLUE_CODES = ("send_in_progress", "switch_pending", "test_running", "action_in_progress")
VIEW_CODES = BLUE_CODES + ("checking", "not_monitored", "update_stripes", "not_set_up")
NEEDS_SEND_CODES: tuple[str, ...]     # §5.3
WORST_ORDER = (RED, AMBER, BLUE, GREY, GREEN)
INK_NOT_DETECTED, INK_NORMAL, INK_LOW, INK_CRITICAL, INK_NONE = 0, 1, 2, 3, 4
ES: dict[str, str]                    # §10

@dataclass(frozen=True, kw_only=True)
class AlertKind:
    kind: str
    sound: bool
    colour: str                       # RED, AMBER or "" (doesn't colour the machine)
    roles: tuple[str, ...]            # "sender", "notify", "last_sender", "floor", "admins", "monitor"
    text: str                         # the N_ text of Alert.msg (low_ink and setup_problem pick by case)
    ref_from: str = ""                # "send_id", "run_id" or "" (time-based id)
ALERT_KINDS: dict[str, AlertKind]     # §4.1

@dataclass(kw_only=True)
class Facts:                          # §3.2
    reachable: bool
    problem: str = ""
    printing: bool | None = None
    running: RunningMessage | None = None
    ink_level: int | None = None
    clock_offset_s: float | None = None
    daily: dict = {}
    foreign_client: bool = False
    running_unchecked: bool = False
    failures: int = 0
    message_name: str | None = None
    bag_count: int | None = None
    bags_recent: int | None = None
    ink_name: str | None = None
    head_temp_c: int | None = None

@dataclass(frozen=True, kw_only=True)
class AlertNeed:
    kind: str
    ref: str = ""
    colour: str
    msg: Msg
    roles: tuple[str, ...]

@dataclass(kw_only=True)
class Outcome:
    code: str
    values: dict = {}                 # headline values
    details: list[Msg] = []
    alerts: list[AlertNeed] = []
    def key(self) -> tuple: ...       # (code, frozenset of "kind:ref")

def facts_from(prev: PollStatus | None, reading: Reading) -> Facts
def prev_facts(prev: PollStatus | None) -> Facts          # Facts(reachable=False) when prev is None
def classify(facts: Facts, prev: PollStatus | None, expected: ExpectedState | None,
             lock: LockInfo | None, machine: Machine, line: Line, data: HxData, *,
             acked: dict[str, Ack] | None = None, standalone: bool = False, at: float = 0.0,
             timings: Timings = TIMINGS) -> Outcome      # §3.4, §3.5
def evidence(old: Facts, new: Facts, timings: Timings = TIMINGS) -> list[str]   # §3.8
def expected_code(expected: ExpectedState) -> str | None                       # §5.1
def norm_name(name: str | None) -> str                                         # §3.3
def matches_expected(running: RunningMessage | None, *, name: str, slot: int | None,
                     fingerprint: str) -> bool                                 # §3.3
def was_printing(prev: PollStatus | None, company: Company) -> bool             # §3.7
def alert_id(machine_id: str, kind: str, *, ref: str = "", at: float = 0.0) -> str   # §4.2
def resolve_recipients(roles: tuple[str, ...], *, sender: str, notify: str,
                       last_sender: str) -> list[str]                          # §4.3
def can_acknowledge(identity: Identity, company: Company) -> bool               # §4.5
def effective_origin(poll: PollStatus | None, expected: ExpectedState | None) -> str   # §5.3
def checked_text(age_s: float | None) -> Msg                                   # §5.7
def board_banner(board: BoardView) -> Msg | None                               # §5.7
def natural_key(name: str) -> list                                             # §5.5
```

Optional fields added to `Reading` (architecture §6.2):

```python
    failures: int = 0                     # failed requests in a row (§6.6)
    problem: str = ""                     # "" | "wrong_printer" | "login_failed"
    bags_recent: int | None = None        # §6.7
    foreign_client: bool | None = None    # §6.8; None: not computed (carry prev)
    running_unchecked: bool | None = None # §6.5 message check; None: not computed (carry prev)
    daily: dict | None = None             # a daily check completed this cycle (§6.10)
    running_reset: bool = False           # R no longer describes the printer and no Load succeeded
    acked: dict[str, Ack] = {}            # acks found for prev's active alerts
    standalone: bool = False
    pending_code: str = ""                # second reading: the first reading's candidate code
    pending_alerts: tuple[str, ...] = ()  # ... and its "kind:ref" alert keys, sorted
```

Optional fields added to `Judgement` (architecture §6.2):

```python
    facts: Facts | None = None            # what to write in P (§3.8)
    candidate: MachineSnapshot | None = None
    candidate_alerts: tuple[str, ...] = ()   # sorted "kind:ref" keys of the candidate
    changed: bool = False                 # the committed code differs from prev's
    started: list[Alert] = []
    ended: list[Alert] = []
```

### 11.2 Fields added to hx_model status classes

`PollStatus` (all optional): `problem: str = ""`, `expected_counter: int = 0`, `reread_seen: str = ""`,
`unreachable_since: float | None = None`, `running_ok_at: float | None = None`, `running_unchecked: bool
= False`, `dst: dict = {}`, `standalone: bool = False`.

`Alert` (all optional): `colour: str = ""`, `ref: str = ""`, `acked_by: str = ""`,
`acked_at: float | None = None`, `ended_at: float | None = None`, `waiting: list[str] = []`.

`MachineSnapshot`: nothing added.

These are written into architecture.md §5.8's blocks, which the `hx_model` builder follows, and into
storage.md §5.2's field list.

### 11.3 hx_poller

```python
FOREIGN_MIN, FOREIGN_WINDOW_S, FOREIGN_CLEAR_S, MAX_BAGS_PER_S, DAILY_S, DST_KEEP_S, ACK_RETRY_S  # §6.1
UNCHECKED_LOADS, UNCHECKED_SLOW_SETS, UNCHECKED_CONFIRMS                                 # §6.5
def dst_change_id(at: float, new_offset_s: float, *, offset_fn) -> str                   # §6.11

class Poller:                         # additions to architecture §6.8
    # __init__(..., local: LocalStore | None = None): in architecture's signature; local is the
    # LocalStore for ReachMemory. None (tests): an in-memory ReachMemory that can always poll
    def seen_polls(self) -> dict[str, tuple[PollStatus | None, float]]: ...
        # machine_id -> (P as the coordinator last read it, clock.monotonic() of that read)
    def last_update(self) -> PollerUpdate: ...
    # local_statuses() (architecture §6.8): in standalone mode the in-memory statuses (§6.12); in
    # share mode the P each of this PC's workers last wrote


class StatusWatcher:                  # additions to architecture §6.8
    def acknowledge(self, alert_ids: list[str]) -> None: ...   # §7; any thread; never blocks
```

`hx_ui.make_services` passes `local=` to `Poller` (ui.md).

## 12. Tests

### 12.1 test_hx_state.py (pure, `FakeClock` not needed)

Helpers build records for "Line 1" / "Line 1 Bagger 1" (delay machine) and an encoder machine, a
`Company` with `bag_counter="PMCNT"`, E in each mode, `RunningMessage`s for an "old" and a "new"
message, and stale `LockInfo`s for every purpose.

1. **Tables**: `STATE_CODES` has exactly the codes of §2 with those colours; every code in
   `PRECEDENCE` and `NEEDS_SEND_CODES` is in `STATE_CODES`; `PRECEDENCE` holds every §2.2 code except
   `internal_error`, once each;
   every `STATE_TEXTS` and alert text has Spanish in `ES`; `ES` doesn't conflict with `i18n.ES`
   (importing `hx_state` doesn't raise); no `Msg` built by `hx_state` has bag text among its values
   (checked with origin lines "Product of USA and Canada" in every input).
2. **The state table, row by row**: one table-driven test with at least one case per code of §2.2
   giving `classify(...)` inputs and the expected code and alert kinds, including: confirmed/notes/
   unconfirmed; drift by fingerprint with the same name; drift by name; no drift when R is None; no
   drift for a rejected or after-switch-failed machine; `printing_off` only after a read-on, otherwise
   `not_printing`; `printing_on_unexpected` for stopped and test-finished; `stop_failed` vs
   `stop_requested` vs `stopped_needs_send` vs `stopped`; `failed_before_switch` with a drift alert as
   well; `left_red`; `rejected` then `stopped_needs_send` with the same `send_rejected` alert key; a
   rejected E followed by a Send that failed before the switch gives `failed_before_switch` with
   `send_failed` only (no `send_rejected`); ink
   levels 0 to 4 printing and not printing; clock offset 119 s (nothing) and 121 s (`clock_off`); a
   first-ever Send failure with mode `none`; `not_from_stripes` and `idle`.
3. **Crash recovery** (§3.6): every row of the table, plus: a confirmed E with a stale send lock of the
   same id is not interrupted; nor is a rejected E with a stale send lock of the same id (it gives
   `rejected` with `send_rejected` only); a recorded failure of the same id gives the failure code; an older
   failure plus a newer interrupted send gives `interrupted_old`; no lock with an orphan
   `switch_pending` behaves as a stale lock; stale test lock with printing on/off; stale stop lock adds
   the detail.
4. **Untrusted facts**: a drift alert is carried with the same id while unreachable, and ends when
   acknowledged while unreachable; `went_silent` vs `off` by `bags_recent` (0 and 5) and by
   `bag_counter == "none"`; acknowledged `went_silent` gives `off`; `wrong_printer` and `login_failed`
   give `setup_problem` without sound.
5. **Two-reading rule** (§3.8): a first reading with PRIN 0 keeps `running_confirmed` and asks for
   `["PRIN"]`; a second reading with PRIN 1 keeps it with no recheck; a second reading with PRIN 0 and
   the matching pending key commits `printing_off` and starts the alert; a second reading that suggests
   a different code commits nothing. A confirm (E only) commits at once. A change of `foreign_client`
   alone commits at once. An unreachable third failure asks for `["reach"]`. `since` is kept while the
   code stays.
6. **Alert identity**: ids for ref and time-based kinds match the §4.2 examples and storage.md's
   pattern; a need continues prev's alert across a "takeover" (prev from another poller key); a new
   occurrence after an end gets a new id; `started` and `ended` lists; `acked_by` copied from
   `reading.acked`.
7. **Recipients**: roles resolve as §4.3; an empty notify user is skipped; `same_user` de-duplication
   keeps the first spelling.
8. **Views**: blue codes for each lock purpose (and `switch_pending`); a lock aged 59 s is blue and 60 s
   isn't; `checking` → `not_monitored` at 30 s with P missing; `not_monitored` at `poll_age_s` 30.0 (and
   29.9 still monitored); NOT MONITORED keeps RED/AMBER and greys GREEN; the "E not judged yet" rule
   (counter mismatch shows `unconfirmed` while P says `running_confirmed`); RED alert colours a GREY
   code and takes the headline; AMBER warning turns GREEN amber but not BLUE; every button flag of
   §5.3, including Resume refused for rejected, resume-blocked, unconfirmed and failed; `effective_origin`
   for a pre-printed film, a drifted message and a name mismatch in standalone. An unjudged E (counter
   6 or 7, `P.expected_counter` 5, P.running the old message): unconfirmed gives AMBER with origin
   `""`; confirmed gives AMBER `confirmed_with_notes` with origin `""` (never GREEN, never the old
   origin), also on the floor row; P already holding the new message stays GREEN with its origin; a
   failure before the switch and a Stop keep R's origin; a judged drift keeps R's origin. The first
   Load of the new message after a Send asks for a second Load (§3.8).
9. **Lines and board**: `worst` order and empty list; line colour = worst machine; `origins_differ` with
   two printing origins (and not when one machine is stopped); its AMBER and headline; "NOT MONITORED"
   line headline; `natural_key` ordering ("Line 2" before "Line 10"); `not_set_up` for a missing,
   inactive and tokenless printer.
10. **Floor rows**: one row per machine in board order; colour and status raised for differing origins;
    `checked_age_s` passed through.
11. **Sounding**: `sounding_alerts` returns an unacknowledged sound alert for its user (case-insensitive
    name) and for `floor_board=True`, never a warning, never an acknowledged one, never one for another
    user, de-duplicated, oldest first.
12. **Texts**: `checked_text(None)`, `(0)`, `(119.9)`, `(120)`; `board_banner` in the three cases.
13. **The message check** (C11b): E running and confirmed with R None and not standalone gives
    `message_unchecked` (AMBER) without an alert; with `running_unchecked` True it also raises
    `message_unreadable` (no sound, AMBER) to the notify user, `admins` and `monitor`; a later reading
    with R set and `running_unchecked` False gives `running_confirmed` and ends the alert; in standalone
    the same E with R None gives `running_confirmed`. `effective_origin` is `""` while P's code is
    `message_unchecked`, and also for `ink_out` with `running_unchecked` True. Second review, 31 Oct:
    a carried `running_unchecked` (C11d) in standalone gives `unknown_while_down` (AMBER, origin `""`),
    keeps `message_unreadable`, and stays so with another MSGNAM and after the printer was off; a
    higher code still wins; a status with nothing carried is judged by its name as before.
14. **Origin after a failed content check**: E `switch_pending` with a fingerprint, a failure after the
    switch, and R with the new name and slot but a different origin TXT and fingerprint:
    `effective_origin` is R's origin line, not E's; a pre-printed film (R.origin_line `""`, matching
    fingerprint) shows E's "USA (pre-printed)"; E with an empty fingerprint never uses rule 1. With E
    `switch_pending` and an after-switch failure (and again with `left_by` set), `P.running` None and
    `P.message_name` equal to `E.message_name`: `effective_origin` is `""`, not E.origin_line. The same
    for `interrupted_unknown` (stale send lock, R None) and for a standalone status with MSGNAM equal
    to the new name.
15. **The sender**: E with `sender` `EXAMPLE\jsmith` rewritten by `EXAMPLE\akim` (`written_by` akim, a
    clock set): `confirm_pending_by` is jsmith, a new `drift` alert's last-sender recipient is jsmith,
    and `stopped` shows E.stopped_by.
16. **Stops after a test**: E `stop_requested_at_printer` with `stop_reason "test"` and printing read off
    gives `test_finished` (in `NEEDS_SEND_CODES`), not `stopped`; with printing on, `stop_failed`; E
    `stopped_by_user` with `stop_reason "test"` gives `test_finished`; `expected_code` agrees.
17. **Other lock purposes**: a fresh lock with purpose `unknown` (a damaged lock) or `"future"` gives
    BLUE `action_in_progress` with the headline key `action_in_progress:other`.
18. **Not from Stripes**: no E and printing on gives `not_from_stripes` with the no-sound warning
    `not_from_stripes` to the notify user only; printing off ends it.
19. **A known drift in standalone** (C11c, owner decision 31 Oct): prev with the code `drift`, its alert
    and a wrong R, and the same E counter: a standalone reading keeps `drift` and the same alert id,
    acknowledged or not, also when the printer doesn't answer (the code is then the printer's); MSGNAM
    naming another message (or E's name when R's differs) gives `unknown_while_down` with the alert
    open, the view RED, sounding, origin `""`; MSGNAM naming R's message again gives `drift`.
    `effective_origin` is R's origin while MSGNAM names R's message, else `""`. Not carried for a newer
    E, a stopped or rejected machine, or a failure after the switch; standalone never raises a drift of
    its own; share mode decides from R as before. Second review, 31 Oct (C11e): in share mode a failed
    required Load (R None, `running_reset`) after an acknowledged drift commits `message_unchecked` with
    the same drift alert and ack, no alert ended, the view RED, quiet, origin `""`; a Load of R again
    gives `drift` with that alert, a confirmed Load of E's message green; not for a newer E. Carried into
    standalone with R None: `unknown_while_down`, the alert open, RED, sounding, origin `""`.

### 12.2 test_hx_poller.py (FakeWorld, FakePrinter, scaled timings or FakeClock)

Threaded tests use `TIMINGS.scaled(0.02)` with a fresh `GateRegistry`; none sleeps more than 0.5 s
of real time.

1. **Budget**: one PC, one fake printer, 10 simulated minutes at steady state: average under 1.0
   request/s, no 5 s window over 15 requests, `max_in_flight == 1`, no `violations`, no write in
   `writes` except the DST case.
2. **P every tick**: the counter goes up every `poll_fast_s` while polling, while paused for a lock,
   and while backing off.
3. **Pause for a lock**: a lock created by another store: within one tick P shows its token, and the
   fake sees no request until release; after release the first cycle has HXUI and a Load.
4. **Drift needs two Loads**: change the fake's slot 9 content behind Stripes' back (same name):
   `drift` is committed only after two Loads disagree; a single `web_reply_hook` that corrupts one Load
   commits nothing. Owner decision, 31 Oct: a confirming Load that fails once gives `drift` about one
   cycle later (the retry, §6.4), not a slow set later; so does a failed first Load (the slow set's Load
   fails right after the change). A single failed Load of an unchanged message: one retry, still green,
   no alert, then the slow set's pace. Every Load failing: amber `message_unchecked` about one cycle
   after the first failure, then one Load a slow set. Every confirming Load failing (every second Load
   junk): amber `message_unchecked` with `message_unreadable` and origin `""` within
   `UNCHECKED_CONFIRMS` cycles of the first good Load that read the other origin, never green after it,
   and the Loads back at the slow set's pace; working Loads then give `drift` (never `drift` from
   single Loads: two must agree). 40 % of Loads failing at random (10 seeds): never green longer than
   `UNCHECKED_CONFIRMS` cycles after the first good Load that read the other origin, red or amber
   within about 20 s of the first Load after the change, and within a slow set plus 15 s of the change.
   A second reading whose PRIN gets no reply (so its Load is never made) commits neither the first
   reading's PRIN nor its R. Second review, 31 Oct: the retry isn't a required Load, so two Loads
   failing in a row after an acknowledged drift keep `drift`, the same alert id, its ack and R's origin
   (only `message_unreadable` starts); with 40 % of Loads failing for 15 minutes after it (3 seeds) the
   code stays `drift` with that one acknowledged alert.
5. **A stray PRIN**: `override("PRIN", "0")` for one reply: no state change; for two in a row:
   `printing_off` with its alert.
6. **Unreachable**: `go_silent()` after `print_bags(10)`: `unreachable_printing` and `went_silent` after
   3 failed cycles plus the second reading; without bags: `off`, no alert; backoff attempts at 5, 10, 30,
   60, 60 s (`FakeClock`); answering again restores the state after the second reading.
7. **Acknowledged went_silent**: an ack file makes the machine `off` and ends the alert.
8. **Crash recovery**: a stale send lock (counter not refreshed for 60 s) with E `switch_pending` and the
   fake running the new message: `interrupted_new`, AMBER, `send_interrupted` alert to the lock's user
   and the notify user; running the old one: `interrupted_old`; printing off: `interrupted_unknown`; the
   new name in the expected slot but different content (a `web_reply_hook` changing a CTEXT TXT of
   every slot 0 Load): `interrupted_unknown` (RED), not `interrupted_new`. A stale test lock with
   printing on: `test_orphaned` and `test_still_printing`.
9. **Ink and clock**: INKL 2 gives `low_ink` without sound; INKL 4 while printing gives `ink_out` with
   sound; a fake clock offset of 200 s gives `clock_off`.
10. **Foreign client**: three `mixup`/`leave_in_buffer` foreign web replies within 10 minutes raise
    `foreign_client`; 30 quiet minutes clear it.
11. **Daily check**: runs one item per cycle, logs `daily_check`; changing the fake's `ENWHD` raises
    `encoder_changed` after the second reading; recording the new value on the machine (revision + 1)
    reruns the check and ends it.
12. **DST**: the `TimeSource` built with an offset function `fn` that changes once (EDT to EST at 1
    Nov 2026 06:00 UTC), and the fake built with the same one (`FakeWorld(printers=[{"offset_fn":
    fn}])`, protocol.md §1.2), so its clock, which never follows daylight saving, is an hour off after
    the change whatever the host's time zone: exactly one TD write per printer, only
    when no lock is held (a lock held at that moment delays it to the next free cycle, and the TD is
    preceded by its own lock check); DATE and TIME are read before and after it; the `dst_clock_set`
    entry has the §6.11 item 5 fields with `ok` True and a `clock` record whose `after` is within 90 s;
    `P.dst` has change `20261101-18000`; a second poller taking over doesn't write again; a TD answered
    `3` is recorded `ok` False and not repeated. Catch-up: a poller started 2 hours after the change
    (no `P.dst`) still makes the one TD; started 25 hours after, it makes none. Skipped: when the fake's
    clock is already right (`clock_offset_s = -3600` set after the change), no TD is sent and `P.dst`
    records `skipped` True. P carries `dst` with
    `sent` True before the TD reaches the fake (checked from a callable `override("TD", ...)` that reads
    poll.json); a P write that fails after that record (`FlakyFileOps` failing the poller's writes from
    then on) stops the worker, and the poller that takes over sends no second TD. No poller worker calls
    `TimeSource.now()` (patched to fail the test); only the `hx-time` thread does.
13. **Standalone**: `FlakyFileOps(down=True)` with `set_monitor_open(True, False)`: after
    `lock_stale_s`, mode `standalone`, checks every `standalone_poll_s` with quick items only (no Load
    in the fake's `requests`),
    `local_statuses()` filled, no drift alert; `refresh_now()` twice within 10 s returns True then
    False; without the monitor open the mode is `idle` and no request is made. Owner decision, 31 Oct:
    a same-name drift known in share mode (from this PC's own worker, and on a PC that only watches,
    from its coordinator's last read) stays red `drift` for the whole outage with the same alert id,
    its ack (this PC's own, or one its watcher read from the share before the outage), R's origin and
    its sound, on every board sample from the share going down, over the change of mode and the
    seconds before the first standalone check, until share mode judges it (card, floor row, line card,
    `sounding_alerts`), also when the message is put back at the printer; another message selected at
    the printer gives amber `unknown_while_down` with the alert open (the view stays red, origin
    unknown); share mode then clears it with two Loads; a drift judged against an older E isn't
    carried, and a PC that never read P starts its checks afresh. No Load in standalone. Second
    review, 31 Oct, on every board sample (card, floor row, line card, `sounding_alerts`) from the share
    going down: a drift whose two last Loads failed stays red and ringing, origin `""`; a carried,
    acknowledged drift whose first share-mode Load fails after the share is back keeps its alert id and
    ack (red, quiet, never E's origin) until two Loads give `drift` again; a drift whose R a failed Load
    left unknown is carried too (red, ringing, origin `""`); a message share mode couldn't check (every
    confirming Load failing) stays amber `unknown_while_down`, origin `""`, `message_unreadable` kept,
    until share mode's Loads give `drift`; with 40 % of Loads failing (10 seeds) and the share going
    down at the first red or amber, no sample is green or shows E's origin.
14. **Waiting recipients**: an alert to a notify user with no heartbeat logs `alert_waiting` once; a
    heartbeat for that user (another store) logs `alert_reached`.
15. **Handover and stop**: `stop()` writes `yielded_to` and removes the heartbeat; a returning rank-0 PC
    starts only after `yielded_to` names it.
16. **Internal error**: a patched `hx_state.judge` that raises once gives `internal_error` and an
    `internal_error` log entry; the next tick recovers.
17. **Watcher and acks**: `acknowledge()` silences `sounding_alerts` on this PC's next board at once,
    writes the ack file, and a second store's watcher sees it within one `watch_s`.
18. **Message check**: a `web_reply_hook` that breaks every slot 0 Load turns a `running_confirmed`
    machine AMBER `message_unchecked`, with `message_unreadable`, within 3 simulated minutes, and its
    `MachineView.origin_line` is `""`; removing the hook brings it back to green at the next slow set.
    With the hook still in place, a handover to a second poller (item 15's setup) and a worker restart
    after a §6.13 suspension each keep the machine AMBER `message_unchecked`: no `running_confirmed` is
    ever committed, the `message_unreadable` id stays the same, and there is no `alert_ended` or second
    `alert_started`.
19. **Ink names**: INKT answering a name not in `Company.inks` once (then a listed one) uses the listed
    one and counts one nonsense event; answering the same unknown name twice keeps the previous
    `ink_name` and counts nothing.
20. **Keeping P fresh**: with `latency_s` such that every read takes about 0.4 × `stale_s / 2` (scaled),
    P's counter never stands still for `stale_s / 2` while the worker is polling, and no request starts
    that couldn't end within `stale_s / 2 + http_timeout_s` of the start of the last P write (checked
    from the fake's `requests` times against the store's P write times). Two more cases, each asserting
    that no fake request ends later than `p_ok + stale_s / 2 + http_timeout_s`: (a) `FlakyFileOps`
    making only lock reads take just under `stale_s / 4`; (b) a Stop on the same store taking the
    printer's gate while the worker is in `read_lock` (the worker's call waits no later than its
    deadline and then sends nothing, `PrinterBusy`).
21. **Standalone delay**: `FlakyFileOps(down=True)` with the monitor open: the mode stays `idle` for
    `lock_stale_s` and then becomes `standalone`; each check reads HXUI, PRIN, MSGNAM and the counter.
22. **Probe**: with no live poller for a printer and this PC without reach, the coordinator probes it
    through `probe()`: the fake records no request; with the printer's gate held by another thread the
    probe returns None and `probed()` isn't called; a printer that another store polls is never
    probed.
23. **Origin after a failed switch**: a Send fails at confirm_1 (a `web_reply_hook` changing the origin
    CTEXT TXT of slot 0 Loads), then the poller's next Loads are dropped: `MachineView.origin_line` is
    `""` ("(origin unknown)" on the card and floor row) until a Load succeeds, after which it is the
    read-back origin.
24. **Start race**: two stores whose coordinators tick in phase, no poll.json for the printer, and
    `FlakyFileOps.slow_s` large enough that one coordinator tick takes longer than the settle: both
    start a worker, the rank-1 one yields at its rank check before any request (`polling_stopped`
    reason `rank`, P with `yielded_to` naming rank 0), exactly one worker sends requests, and the
    fake's `max_in_flight == 1` with no "two requests in flight" violation.
25. **After a long gap** (§6.3 step 1, §7 item 6): the share comes back after standalone checks found
    printing off, a message changed at the panel, or the printer silent, and again with no monitor
    open: no board sample is GREEN, and none shows the old origin, until a share-mode judgement; an
    unchanged printer is GREEN again after its checks. The first PC started an hour after every PC
    closed, with the message changed overnight: `checking`, then `drift`, never last night's green; the
    message fixed overnight: last night's red until share mode judges it green. A quick takeover keeps
    P's state.
26. **Acks racing a pass** (§7 item 5): an `acknowledge()` while the watcher's pass builds its board
    (from `board_view`, and with real threads during a slow lock read) leaves every later board quiet;
    a pass that ends inside `acknowledge()` leaves `latest` and the last `BoardUpdate` as the newer
    board, with the ack applied.

### 12.3 test_hx_sim.py (shared with storage.md and protocol.md)

The file is written by WP6 (README.md "Work packages"), with the storage.md and protocol.md cases;
it needs `hx_send` (WP5) for the Sends from two PCs.

States-related assertions: with every poller gone, every machine is `not_monitored` on a remaining
watcher after 30 s (scaled); a takeover keeps alert ids (no second `alert_started` for the same
occurrence); an acknowledgement on one simulated PC silences `sounding_alerts` on every other within
one `watch_s` (scaled); a sender confirming the moment a Send ends never sees the previous origin, nor
GREEN with an origin the fake isn't printing, in the views sampled until the poller commits.

Two cases from the review:

1. **Slow everything**: one `FakePrinter(latency_s=1.9 × f)`, a share on `FlakyFileOps` with `slow_s =
   3 × f`, a silent `FakeSntp`, a rank-0 poller PC, a rank-1 PC and a sender on a third store sending
   twice, with `TIMINGS.scaled(f)`: `max_in_flight == 1` and no "two requests in flight" for the
   whole run.
2. **One PC cut off**: PC A on `world.fs.view(down=True)` with its monitor open, PCs B (polling) and C
   (sending) on normal views, `mixup` on: A polls standalone only after `lock_stale_s`; every Send of C
   ends with read-backs that matched their own TN and XID (`MessageDesign.tn` and `xid`), and no Send
   is green with a content the fake didn't hold. Overlapping requests are allowed in this case
   (architecture §7.3 rule 7).

## 13. Notes for the other spec files

**send.md** (what this file relies on in `ExpectedState`):

- `switch_pending` is written before PRINTMSG with the new `message_name`, `slot`, `content_code`,
  `fingerprint` (`hx_message.fingerprint` of read-back 1 of the idle slot, so crash recovery and
  `effective_origin` can tell the new message by content; send.md §14 row "Switch pending"),
  `origin_line`, `sender`, and `previous_message_name`/`previous_fingerprint` of what was printing.
- `running` (after both confirm reads) has `fingerprint` from read-back 3, `printing_expected` True,
  `confirmed` False, and `notes` for amber reasons.
- A failure before the switch keeps the mode and the previous message's `message_name`, `slot` and
  `fingerprint`, so drift is still judged against what is printing. It sets `rejected` False and keeps
  `resume_blocked`, so an earlier rejection doesn't raise a second `send_rejected` next to the new
  `send_failed`.
- Stop writes `stopped_by_user` (with `rejected`/`resume_blocked` after "Something's wrong"); a failed
  Stop writes `stop_requested_at_printer`; Resume writes `running` with the resumed message; a test
  writes `test_running` with `test = {"run_id", "by", "step", "of"}` and `test_finished` at the end.
- Confirm and Leave it take the lock with purpose `send` and `run_id = send_id`; Leave it sets a new
  `reread_token`.
- `sender` names who made the Send or test E belongs to; this file uses it for "the sender", "last
  sender" and `confirm_pending_by`, and `stopped_by` for the `stopped` headline (send.md §1.4).
- `stop_reason "test"` with mode `stop_requested_at_printer` or `stopped_by_user` is judged like
  `test_finished` (C17).
- A Send made with a `local_only` lock (share down) can't write E; send.md §14.1 records it when the
  share comes back (`record_offline`), so this file does nothing special for it.
- `hx_time`: `TimeSource.cached()`, `offset_fn` and `cache_s`, and `recent_dst_change` returning `(old,
  new, at)` (send.md §1.2, §9.2, §9.3), which §6.2 item 6 and §6.11 use.

**ui.md**: call `Poller.set_monitor_open(True, ...)` while the MapleJet tab is shown, the MapleJet monitor
is open or the floor board is open; make `assets/alert.wav` to §4.4's cadence; use `checked_text`,
`board_banner`, `can_acknowledge` and `StatusWatcher.acknowledge`; draw §8; pass `local=` to `Poller`;
don't register different Spanish for §10's texts.

**protocol.md**: the poller uses `read(tag, deadline=...)` (HXUI included, never `check_uid` or
`read_twice`), `command(cmd, deadline=...)` (the daylight-saving TD), `load_slot(0, deadline=...)`,
`fonts(deadline=...)`, `firmware(deadline=...)`, `probe()` (None when the
gate is busy), `login`, `worst_case_s`, `nonsense` and `busy_replies` as specified there.

**storage.md**: the poller follows §11.6 and §12 there, probes only printers without a live poller
(§12.3), and needs no poller memory file: daily checks are in P.daily and the daylight-saving clock
set in P.dst, under a change id every PC recomputes (§6.10, §6.11).

## 14. OPEN items

| # | Item | Safe default built now |
|---|---|---|
| 1 | Which counter counts bags (architecture §11) | `Company.bag_counter = "PMCNT"`. If PMCNT doesn't count bags, `bags_recent` stays 0 and a printer that dies while printing shows grey `off` instead of `unreachable_printing`. Confirm with `print_bags` on the test unit; `"none"` makes `was_printing` rely on PRIN alone |
| 2 | Power cycle: which message loads and whether printing is on (plan, still to test) | After the printer comes back, printing off gives amber `not_printing` (no sound) and the first Load is checked for drift. If the test unit shows printing comes back on by itself, nothing changes |
| 3 | Shift-end switch-offs inside `silent_window_s` (300 s) | `went_silent` fires if a printer is switched off within 5 minutes of its last bag; acknowledging turns it into `off`. Lower `silent_window_s` if the floor finds this noisy |
| 4 | INKL meanings on the real printer (0 = not detected treated as a cartridge error) | As the manual says: 0 cartridge error, 2 and 3 low (no sound), 4 out of ink |
| 5 | Whether PRIN reads 0 when the printer stops for an error (ink out) | Both alerts may fire; the precedence shows `printing_off` first |
| 6 | SMB not-found cache delaying a new ack file | Acks are read by name every `watch_s`; worst case about 10 s instead of 5 (storage.md §19) |
| 7 | Encoder wheel units (`ENWHD` in mm, as an int) | `encoder_wheel_mm` rounded to an int for the comparison |
| 8 | Beacon | Not used (`BEACON_SUPPORTED = False`): no beacon state, query or switching |
| 9 | Whether the floor board's 12 rows are readable at the floor distance | Rows fill the screen equally; ui.md may enlarge the origin type |

## 15. Decisions made in this spec

1. **Three layers, one decision point.** Only `judge()` turns printer readings into codes and alerts;
   views only overlay a fresh lock, staleness, an unjudged E and acks.
2. **The two-reading rule is applied to outcomes**, not to raw values: a reading that would change the
   code or the set of alerts is confirmed by re-reading only the facts that caused it. Changes caused
   only by E, a lock or an ack commit at once (they need no printer evidence).
3. **P stores committed facts** for everything judged and the latest values for display facts, so an
   unconfirmed reading can never become "the previous state".
4. **Drift is decided only by web Loads** (they carry request numbers); a MSGNAM mismatch only triggers
   a Load. Drift isn't judged on stopped machines (printing on is alerted instead).
5. **"Printing switched off" needs an observed read-on first.** A printer that comes back from being
   off with printing off is amber `not_printing` without sound.
6. **Printing switched on while Stripes expects it off** (after a Stop or a test) is a red alert with
   sound, `printing_on`, the mirror of the plan's "switched off" alert.
7. **`went_silent` ends when acknowledged** while the printer is still silent, and the machine shows
   grey `off`; any alert that needs printer readings ends when acknowledged while the printer can't be
   read, and is raised afresh from new readings when it answers.
8. **Alert colours**: a red alert makes any machine red; an amber warning only turns green or grey
   amber, so warnings never hide a blue test or a red failure. Low ink and a clock off therefore show
   amber on the floor board.
9. **NOT MONITORED never makes a machine look better**: it keeps a last known red or amber and greys
   only green, blue and grey.
10. **An E the poller hasn't judged yet is shown at once** (`expected_counter` in P), so a card never
    shows the old message as green right after a Send. Until the poller has judged it, an R that
    doesn't match E shows the origin as unknown and caps the colour at amber, also after a quick
    confirm (owner-approved divergence, 31 Oct).
11. **Alert ids** are `<machine>.<kind>.<send or run id>` for alerts tied to a Send or test, and
    `<machine>.<kind>.<start ms>` otherwise; recipients are fixed when the alert starts; alerts continue
    across a poller takeover.
12. **Leave it doesn't start a new alert**: the `send_failed` alert continues until acknowledged, and
    the line stays red (`left_red`).
13. **"Something's wrong" and "Stopped, needs a Send" share one `send_rejected` alert** to the sender
    and the notify user, so closing the window keeps the notify user alerted without a second ring.
14. **Stop failed** is one alert (`stop_failed`, with sound, to the person who tried to stop, the notify
    user and the floor board) behind two codes: `stop_requested` until printing is read, `stop_failed`
    when it is read still on.
15. **A test that finished is red "Stopped, needs a Send"** (as the plan's wording), without sound.
16. **Interrupted Sends**: a stale send lock whose Send never recorded "switch pending" is red
    `interrupted_old` whatever is printing, because PRINTMSG was never sent (send.md §10.9 refuses to
    switch when that record can't be written under a held lock); an orphan `switch_pending` without any
    lock is treated like a stale lock; "the new message" needs the idle-slot fingerprint too, so a
    message with the new name but other content is `interrupted_unknown`.
17. **A stale stop, resume or clock lock** is judged normally with a detail; a resulting
    `printing_off` alert is accepted.
18. **Ink**: INKL 4 and 0 sound only while printing; otherwise they are amber warnings.
19. **Encoder check** compares with the machine record's values, or with the first daily reading when
    the record has none; the alert has sound (an encoder change moves every print).
20. **Font-list differences** are a warning to administrators; a missing list raises nothing here,
    because the Send refuses to send anyway.
21. **Unreachable timing**: each failed request ends its cycle, so three failed cycles plus the second
    reading take about 20 s; a few seconds' dropout raises nothing.
22. **"Something else is talking"** = 3 nonsense or busy events within 10 minutes, cleared after 30
    quiet minutes; not judged in standalone mode; implausible bag-counter jumps and inconsistent ink
    names count as events, but an unknown ink name the printer reports consistently doesn't.
23. **Daily checks** spread one item per cycle, rerun when the machine record changes, and record
    `encoder_ref` in P.
24. **The daylight-saving clock set** is recorded in P under a change id every PC computes the same
    way from `recent_dst_change`, so it happens once per printer per change even across takeovers and
    restarts (a PC started up to a day after the change still makes it), and waits up to 24 hours for
    a printer that is off. It reads the clock first and sends no TD when the clock is already right,
    and verifies the clock after the TD as a Send does; the TD itself is never repeated, because P
    records it (`sent` True) before it is sent, so a takeover after a crash or a failed write can't
    send a second one.
25. **Standalone mode** judges with the cached E and no locks, skips crash recovery and drift, shows
    `unknown_while_down` when the message name differs from the last Send on record or can't be read
    (and while printing hasn't been read), logs with
    `"standalone": true`, and keeps acks local. Owner decision, 31 Oct: a known origin mismatch is
    seeded from the last P this PC read and stays red, with its alert, acks and the read-back origin,
    at best amber `unknown_while_down` once another message is selected, until share mode clears it
    (standalone has no Loads, so it can neither see a drift nor see one end). Second review, 31 Oct:
    so is a drift whose R a failed Load left unknown (red, the origin unknown), and a message share mode
    couldn't check (`message_unchecked`) stays at best amber `unknown_while_down` with the origin
    unknown, for the same reason. Only those are carried; other states start afresh. The board keeps
    the last P until the first standalone check.
26. **Acknowledging** is per alert; the card buttons acknowledge every active alert of the machine or
    line. The acknowledging PC goes quiet at once; the others within one `watch_s`.
27. **The alert sound** is `alert.wav` looped every 3 s; no snooze; the floor board rings only for
    floor alerts; no sound alert is addressed to administrators.
28. **The waiting log** uses live heartbeats of the same format, logging `alert_waiting` and
    `alert_reached` once per alert and recipient.
29. **Floor-board order is fixed** (lines then machines by natural name), every row shows its own
    origin, and rows of a line whose origins differ are raised to amber with "Different origins on
    this line".
30. **"Printer off"** is the grey state's English text, because "Off" already means "Nunca" in
    `i18n.ES`.
31. **The coordinator reads every P each heartbeat** and the watcher reuses those reads
    (`seen_polls()`), so each PC opens each poll file once per 5 s.
32. **`Poller` takes a `local=` LocalStore** for `ReachMemory` (now in architecture's signature); in
    tests it may be None. No poller memory file is needed.
33. **Internal errors in a poller tick** write red `internal_error` and carry on at the next tick.
34. **A printer running a message Stripes never sent** (no E: today's printers before their first
    Send) is amber `not_from_stripes` with a no-sound warning to the notify user, not the plan's drift
    alert with sound. During rollout every printer starts like this, and ringing for each would teach
    people to ignore the alert; once a printer has had a Send, anything else it runs is `drift` with
    sound.
35. **HXUI is read once a minute** (and after every start, pause or unreachable spell), not every cycle,
    to keep the budget well under one request a second. A printer swapped onto the address is still
    caught at once by MSGNAM: its name differs, which forces a Load whose fingerprint doesn't match
    (drift, red). Standalone checks read HXUI every time.
36. **A running message that can't be read is a failed check**: amber `message_unchecked` with a
    no-sound warning, green needs a successful Load since E last changed, and the origin shows as
    unknown meanwhile. A failed Load, and a confirming Load that fails, are failed checks too (owner
    decision, 31 Oct): the next cycle loads again instead of waiting a slow set, and three failed
    confirmations in a row give amber `message_unchecked`, so a good Load that read another origin
    never leaves the machine green. The two-reading rule is unchanged: amber says only that the check
    failed. Second review, 31 Oct: that retry isn't a required Load, so its failure keeps R, and a
    failed required Load (R unknown) never ends a known drift's alert (C11e): only a Load that works
    and its second reading do. Otherwise flaky Loads would end an acknowledged drift alert and start a
    new one that rings again.
37. **P stays fresh while the poller works**: P is rewritten before any request when 5 s have
    passed, no request may outlive `stale_s / 2 + http_timeout_s` after the start of the last good P
    write (the lock is read before that check, and every request carries the time as its deadline,
    which also bounds the wait for this PC's gate), slow share operations count as failures, and
    workers never wait on the time server (`cached()` plus the `hx-time` thread). So a sender or a
    takeover poller can never start while this poller still has a request in flight.
38. **Standalone checks start after `lock_stale_s` of continuous outage**, so a short outage on one PC
    never overlaps the other PCs' polling; a longer partial outage is the residual risk named in
    architecture §7.3 rule 7.
39. **Nothing old is shown as just checked** (owner-approved divergences, 31 Oct): a worker starting
    after a long gap (a share outage, every PC closed overnight) writes `checking`, or this PC's red or
    amber standalone finding, until its first judgement, and keeps a red or amber P as it is; a watcher
    shows a P it has never seen move, written long ago, as `checking`; and `checking` shows no origin.
    So the old origin is never green "Printing" after a gap, and a red or amber never turns green before
    a share-mode reading.
