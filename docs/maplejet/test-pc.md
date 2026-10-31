# Trying a new Stripes version on one test PC

For IT and an administrator. Do this for every new release before it goes on the share. Each step
says what to do and what you should see ("Pass"). Write the results in the test record at the end
and keep it with the release.

This only accepts the build on a PC. The test-unit engineering checks (fonts, sizes, encoder, units,
stress, soak) are separate.

The examples use made-up names: the test unit "Printer 1" at `192.0.2.10`, UID 1109, on the machine
"Line 1 Bagger 1" of "Line 1"; the tester `EXAMPLE\jsmith`; the share `\\SERVER\Share\Stripes`; the
time server `time.example.local`. Use your own.

## Before you start

1. The two groups exist (`Stripes Administrators` and `Stripes MapleJet Senders`). The tester is in
   both and has signed out and in since. For step 4 you also need one account in neither group and
   one in `Stripes MapleJet Senders` only.
2. The folder script has run, and its summary looked right ([it-setup.md step 6](it-setup.md#6-run-the-folder-script),
   [deploy.md §8.4](deploy.md#84-folders-and-their-explicit-entries)).
3. A release has been made in GitHub Actions with the `STRIPES_HX_KEY` secret. The share still has
   the old `Stripes.exe`.
4. The test unit is on the printer network with a new password, and runs empty film (no product).
   The test PC reaches it on port 80 (`Test-NetConnection 192.0.2.10 -Port 80` says
   `TcpTestSucceeded : True`) and the time server on UDP 123
   (`w32tm /stripchart /computer:time.example.local /samples:3 /dataonly` prints times).
5. For step 14: an office PC that can't reach the printers, where you can run the same test copy.
6. The test PC's speakers are on and the volume is up.

## Steps

### 1. Install side by side

- Download `Stripes.exe` from the new release and, in its Properties, **Unblock** it. Properties >
  **Digital Signatures** shows the same signer as the current release.
- Copy it to `%LocalAppData%\Programs\Stripes-test\` (not the Group Policy folder, which is replaced
  at every sign-in) and start it from there. The old copy can keep running; they don't share files.
- If AppLocker or WDAC allows only the Group Policy folder, allow the release's signed publisher.

Pass: the top right corner shows `Version vX.Y.Z` with no "(development key)", and the window has
two tabs, **Zebra** and **MapleJet**.

### 2. Zebra unchanged

- On the **Zebra** tab, the printer list is the same as in the old copy (the company's printers
  now come from `v2\config\printers.json`). Your own Zebra settings came across from the old copy:
  the **Recent:** labels and any printers you added yourself.
- Print a test label. **Printer monitor** works.
- Ctrl+P prints on the Zebra tab. On the MapleJet tab, and in any MapleJet window, it does nothing.

Pass: all of the above.

### 3. Deployment

- Zebra tab > **Manage printers...** > **Deployment status...**.

Pass: the window lists this PC with the new version, its subtitle names
`\\SERVER\Share\Stripes\v2\status\deployment` as well as the old `\\SERVER\Share\Stripes\status`, and
the other PCs are still listed with the old version. This PC's file (`COMPUTER_user.json`) is in
`v2\status\deployment`.

### 4. Rights, three people

Sign in as each person in turn. Each needs their own copy in their own
`%LocalAppData%\Programs\Stripes-test\`.

- **In neither group**: the MapleJet tab says "Signed in as ... You can watch the lines. To send, ask
  IT to add you to Stripes MapleJet Senders." Its buttons are only **MapleJet monitor**, **Floor
  board** and **My settings...**: no **Send...**, **Set all clocks...** or **Admin...**. Once there
  are lines (step 5; until then it says "No lines are set up yet."), their cards have no buttons.
- **Sender only**: "You can send and acknowledge alerts." **Send...** and **Set all clocks...** are
  there; **Admin...** isn't.
- **Administrator** (the tester, in both): "You can send, change settings, run test prints and
  acknowledge alerts." **Admin...** opens the **MapleJet admin** window.
- In Explorer, as the person in neither group: you can't change, rename or delete a file in
  `\\SERVER\Share\Stripes\v2\logs\hx-events` (if it's still empty, try one in `v2\logs\hx-sends`
  after step 7), and you can't create a file in `v2\config`.

Pass: all of the above.

### 5. Administrator set-up

As the tester, MapleJet tab > **Admin...**, tab by tab
([it-setup.md step 9](it-setup.md#9-first-administrator-steps-in-stripes)):

- **Company**: **Time server** `time.example.local` and its **Fallback**, the countries and the inks.
  **Save**.
- **Printers**: **New printer** "Printer 1", **IP address** `192.0.2.10`, **Port** 80, **UID** 1109,
  and its **Password**. **Save**.
- **Lines**: **New line** "Line 1". **Machines**: **New machine** "Line 1 Bagger 1" on Line 1 with
  Printer 1, its **Placement mode**, **Print speed (cm/min)** and **Seconds per bag** (an Encoder
  machine: **Calibrate...** first).
- **Products and films**: a product (customer "Example Co") and, with **Add new version**, a film.
  **Print texts**: a print text, with the film ticked under **Films that may use this print text**.
- On a later release, with all this set up already: open each tab, and save one small change to
  the test unit's records.
- Then quit Stripes (right-click the tray icon > **Quit Stripes**) and start the test copy again. If
  a second PC runs the same release, open the printer there too.

Pass: the Printers tab says "A password is saved. Type a new one only to change it."; Line 1 Bagger 1
has a fresh "checked N s ago" on the MapleJet tab; the footer says how many printers this PC is
checking ("This PC is checking 1 printer." when only the test unit is set up); and after the restart
(and on the second PC) the printer still logs in, with no "Stripes can't log in to this printer". The
scrambled password works for another copy of the same release.

### 6. Test print on empty film

- **Products and films** > select the film > **Test print...** (it says "Save the film first." until
  the film is saved).
- In the **Test print: ...** window: choose the **Machine** (Line 1 Bagger 1), tick "Line 1 Bagger 1
  is running empty film, with no product.", and set **What to vary** (from, to, in steps of). It
  shows the number of steps and about how many bags.
- **Start test print**, then **Start** in the question.

Pass: the window counts "Step 1 of N" and so on, and the machine's card on the MapleJet tab is blue,
"TEST by ...". At the end it says "Finished: N of N steps." and "Printing is off and slots 9 and 10
are empty. Line 1 Bagger 1 needs a normal Send before production."; the card says "Stopped, needs a
Send"; and the run is logged in `v2\logs\hx-admin`. Then set the film's Status to **Active** and
**Save**.

### 7. Send

- MapleJet tab > **Send...** opens **MapleJet Send** ("Send to the bag printers").
- Under **Lines:**, tick Line 1. In its block, Line 1 Bagger 1 is ticked under **Machines**; choose
  the **Product**, the **Film** and the **Print text**, type the **Lot**, and **Add** the countries.
  **Origin on the bag:** shows the origin in large type.
- Press **Send to 1 machine** (it stays greyed out until everything is filled in; "Still needed:"
  lists what's missing). The window shows "Sending to Line 1", "Getting ready...", then
  "Sending in" with a 5-second countdown and **Cancel**, then "Hang on, we're working on it" with a
  row for the machine.
- The **Check the new messages** window opens with the printer's own picture, "Live and checked
  twice. Waiting for you to confirm." Wait for the first bags, then press **Everything is right**.

Pass: it says "Confirmed by you at ..."; the line's card goes green, "Printing"; and the send log is
in `v2\logs\hx-sends\<year-month>\` (a file with `.send.` in its name).

### 8. Sounds and alerts

- Switch printing off at the printer's own **Print On/Off** button.

Pass: within about 10 s the card goes red, "Printing was switched off at the printer". Because you
sent last, a **MapleJet alert** window opens on the test PC ("Printing was switched off on Line 1
Bagger 1") and the alert sound repeats. **Acknowledge** stops it, and the card says "acknowledged by
...". Then **Resume...** on the machine's row > **Resume printing**: the card goes green again.

### 9. Firewall

Pass: no Windows Firewall prompt appeared at the first Send (the time server, UDP 123) or at any
other time.

### 10. Floor board

- MapleJet tab > **My settings...**: tick **Open the floor board when Stripes starts**, choose the
  **Screen** and the **Size** (**Full screen**, or **Window** with a width and height), **Save**.
- **Open the floor board now**.
- Quit Stripes (tray icon > **Quit Stripes**) and start the test copy again.
- To check sign-in as well: Group Policy's Startup shortcut still starts the old copy, so put a
  shortcut to `%LocalAppData%\Programs\Stripes-test\Stripes.exe` with the argument `--tray` in your
  Startup folder (`shell:startup`), sign out and in, then delete that shortcut.

Pass: the floor board opens on the chosen screen at the chosen size, each time Stripes starts and
after signing in. Full screen has no window frame and Escape does nothing; **Close** closes it.
Untick the setting afterwards if the test PC isn't the floor-board PC.

### 11. Stop, Resume and Set clocks

- On Line 1's card, **Stop line...** > **Stop printing**.
- **Resume line...**: it shows "Loading what's on each printer...", then "This is the message of the
  last confirmed Send." > **Resume printing**.
- **Set clocks...** ("Set the clock of every printer on Line 1 from the time server?") > **Set
  clocks**.

Pass: "Printing stopped on Line 1 Bagger 1." and the card says "Stopped by ..."; then "Printing
resumed on Line 1 Bagger 1." and the card is green, "Printing"; then the **Set clocks** window
shows "Clock set" for Line 1 Bagger 1, with no "The time server didn't answer, so the PC's time was
used."

### 12. Share down

- Make the share unreachable from the test PC only, for a few minutes: for example, block TCP 445
  between the test PC and the file server on the network firewall or the file server. Don't unplug
  the test PC: it must still reach the test unit.
- Open **Send...** and send to Line 1 again, and confirm it.
- Then unblock the share.

Pass: within about a minute the MapleJet tab says "Server unreachable: this PC isn't checking any
printers." (with the MapleJet monitor open, "Server unreachable: showing this PC's own checks · next
check in N s"). The Send window says "Using this PC's copy of the templates, as of ... The server
can't be reached, so every machine's result will be amber." The Send finishes amber, and the result
window says "Sent from this PC's copy of the templates (as of ...)" and "The send log is waiting on
this PC. It uploads when the server is back." If confirming says the server can't be reached,
Stripes keeps your answer and saves it when the server is back. Once unblocked, the banner goes
within about a minute and the Send's log appears in `v2\logs\hx-sends`.

### 13. Tray

- Close the main window with its close box.
- Right-click the Stripes icon in the notification area > **MapleJet monitor**.

Pass: Stripes keeps running in the tray (the first time, a notice says "Stripes is still running.
Click its icon to open it; right-click it to quit."), and the **MapleJet monitor** window opens while
the main window stays hidden. **Open Stripes** brings the main window back.

### 14. Alarm for a machine left NOT MONITORED

A machine that was printing and that no PC checks for 2 minutes sounds an alarm. To test it, stop
Stripes polling the test unit by closing Stripes on the polling PC when no other PC can take over.
That works only while the test PC is the only floor PC running a MapleJet version of Stripes (always
so for the first MapleJet release). Once other floor PCs run one, they take over and NOT MONITORED
never shows: write "not possible" in the record.

- On the office PC, put the same test copy in `%LocalAppData%\Programs\Stripes-test\`, start it, sign
  in as the tester, and open **Floor board**. Its MapleJet tab footer says "This PC isn't checking
  any printers." (office PCs never poll).
- Make sure Line 1 Bagger 1 is printing a confirmed Send (green, "Printing"). Wait until its
  "checked N s ago" has refreshed a few times on the office PC.
- On the test PC: right-click the tray icon > **Quit Stripes**.

Pass: after about 30 s the machine shows NOT MONITORED on the office PC. After about 2.5 minutes in
all, the alarm sounds there, with an alert saying no PC has checked the machine. **Acknowledge**
stops it. Start the test copy on the test PC again: the machine is checked again and NOT MONITORED
goes.

## Optional: two PCs on the new release

With the test copy on a second floor PC as well. Do A before B, so both PCs have reached the test
unit.

- **A. Two Sends at once.** Start a Send to Line 1 on one PC, and on the other start one during the
  first one's countdown. Pass: the second stops at the lock with a red **Send failed** window saying
  "Line 1 is being sent by ...", and nothing is sent from it. After the first Send is confirmed,
  **Try again** on the second PC runs normally.
- **B. Takeover.** On the PC that is checking the test unit (with only the test unit set up, its
  footer says "This PC is checking 1 printer."), quit Stripes (tray icon > **Quit Stripes**). Pass:
  within about 20 s the other PC takes it over (its footer count goes up by one), and the machine
  never shows NOT MONITORED.

## After the steps

1. **Sign-off.** Fill in the test record below and keep it with the release.
2. **Rollout.** Put that same `Stripes.exe` on the share (`\\SERVER\Share\Stripes\Stripes.exe`). PCs
   get it at their next sign-in. Watch **Deployment status**. Delete
   `%LocalAppData%\Programs\Stripes-test` on the test PC (and on the other PCs used).
3. **Rollback.** Put the previous `Stripes.exe` back on the share. The `v2` folder stays. A version
   from before MapleJet ignores it: the MapleJet printers keep printing their last message, and
   nobody can send, stop or resume from Stripes until a MapleJet version returns.

## Test record

Release: ______  Test PC: ______  Test unit: ______  Office PC (step 14): ______

| Step | Result (pass or fail) | Notes | Who | Date |
|---|---|---|---|---|
| Before you start | | | | |
| 1. Install side by side | | | | |
| 2. Zebra unchanged | | | | |
| 3. Deployment | | | | |
| 4. Rights, three people | | | | |
| 5. Administrator set-up | | | | |
| 6. Test print on empty film | | | | |
| 7. Send | | | | |
| 8. Sounds and alerts | | | | |
| 9. Firewall | | | | |
| 10. Floor board | | | | |
| 11. Stop, Resume and Set clocks | | | | |
| 12. Share down | | | | |
| 13. Tray | | | | |
| 14. Alarm for a machine left NOT MONITORED | | | | |
| A. Two Sends at once (optional) | | | | |
| B. Takeover (optional) | | | | |
| Signed off | | | | |
