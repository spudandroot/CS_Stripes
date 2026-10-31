# Setting up Stripes MapleJet: IT checklist

The order to set up the MapleJet version of Stripes, from nothing to every PC running it. Tick each
box as you go. Each step says what you should see. The details live in the root
[README](../../README.md) and the spec files next to this one; this list links to them.

The examples use made-up names: the share `\\SERVER\Share\Stripes`, the domain `EXAMPLE`, printer
addresses `192.0.2.x`, printer UID 1109, the time server `time.example.local`, the customer
"Example Co" and the machine "Line 1 Bagger 1". Use your own.

## 1. Before you start

- [ ] **Accounts.** You need:
  - an account that can create AD groups;
  - an account with Full Control on `\\SERVER\Share\Stripes`, for the folder script (for example
    `EXAMPLE\itadmin`), and a member of the file server's Administrators group (a domain admin)
    for running it again later;
  - rights to edit the Group Policy objects that deploy Stripes today;
  - access to the network firewall and the Wi-Fi controller;
  - a tester (for example `EXAMPLE\jsmith`) who goes in both Stripes groups, plus one account in
    neither group and one in the senders' group only, for the rights check on the test PC;
  - for whoever makes releases: admin access to the GitHub repository (step 14).
- [ ] **PCs.** Sort the PCs that will run Stripes:
  - **Floor PCs**: every PC that sends, stops, resumes, sets clocks, runs test prints, runs the
    MapleJet monitor or is the floor board. They must reach every MapleJet printer (step 2).
  - **Office PCs**: everyone else. They watch the lines through the share and never talk to a
    printer, not even while the share is down.
  - **One test PC**: a floor PC, for trying each release (step 11), and one office PC for its
    alarm check.
  - **One maintenance PC**: the only other PC that may open the printers' own web portal.
- [ ] **Printers.** For each MapleJet printer, write down its name, its fixed address, its UID and
  its new password (in IT's password safe). Keep one printer as the **test unit**, on empty film.
- [ ] **The file server** that holds `\\SERVER\Share\Stripes` today, with `config\printers.json`
  and `config\stock.json`.

You should see: a list of floor PCs, office PCs, the test PC and the maintenance PC, and each
printer's address, UID and password.

## 2. The printer network

Stripes talks to the printers directly, from the PC in use, one request at a time, in plain HTTP on
port 80 ([protocol.md §2.1](protocol.md#21-rules)). The printers check passwords weakly, so anyone
who can reach port 80 on a printer can change what it prints, including the origin. **The firewall
rule below is the real security boundary.**

| Network | What's on it | What it may reach |
|---|---|---|
| Printer VLAN, with its own Wi-Fi network | MapleJet printers (Wi-Fi only) and Zebra printers | Nothing: it only answers |
| Floor-PC VLAN | The floor PCs | The printers, the file server, the time server |
| Office and server VLANs | As today | The file server; never the printers |

- [ ] **Printer Wi-Fi.** A dedicated Wi-Fi network (SSID) for the printers, mapped to the printer
  VLAN:
  - WPA2 or WPA3, with a strong password that only IT knows: a second lock on top of the firewall
    rule.
  - Client isolation on, so printers can't talk to each other.
  - Floor PCs never join it. They stay on their own network and reach the printers through the
    firewall rule. (With client isolation on, a PC on the printer SSID couldn't reach them anyway.)
  - Good signal at every printer: an access point near each line.
- [ ] **Addresses.** Give each printer a fixed IP address or a DHCP reservation on the printer
  VLAN, for example `192.0.2.10`, `192.0.2.11`. Stripes takes IPv4 addresses only, never names.
- [ ] **Nothing outbound.** The printer VLAN has no internet and starts no connections. The
  printers need no DNS and no NTP: Stripes uses their addresses and sets their clocks itself.
- [ ] **The firewall rule into the printer VLAN.** Allow only:
  - TCP 80 to the MapleJet printers, from the floor-PC VLAN;
  - TCP 9100 to the Zebra printers, from the floor-PC VLAN;
  - TCP 80 to the MapleJet printers from the one maintenance PC, for the printers' web portal.

  Nothing else. If office PCs print Zebra labels today, add them (or the print server, for Zebra
  printers installed in Windows through one) to the TCP 9100 rule. The all-or-nothing rule below is
  about MapleJet printers only.
- [ ] **No proxy, NAT or HTTP inspection** between the floor PCs and the printers. Stripes never
  uses a proxy for printers ([protocol.md §0](protocol.md#0-scope-and-conventions) item 3).
- [ ] **All or nothing: every floor PC reaches every MapleJet printer.** Stripes gives a copy of
  Stripes printers to watch once it has reached any printer in the last 24 hours, and from then on
  it may give it **any** printer ([storage.md §12.3](storage.md#123-can-poll-reach-memory-and-the-reachability-probe)).
  That memory belongs to each Windows user on each PC (in the user's `%LocalAppData%`), not to the
  PC as a whole. A copy handed a printer it can't reach reports it as not answering (grey "Printer
  off", or red "Not answering while printing bags" with an alarm), and no other PC takes it over.
  So:
  - never split the MapleJet printers into per-line VLANs that some floor PCs can't reach;
  - office PCs that reach no printer are fine: they never watch printers, and still show the
    monitor and the floor board through the share. While the share is down they check no printers
    either, and say "Server unreachable: this PC isn't checking any printers." Sending, Stop,
    Resume, Set clocks and test prints talk to the printer from the PC in use, so they work only
    from floor PCs (the other admin screens work anywhere);
  - when a printer has no PC checking it, every running Stripes tries a bare connection to it on
    port 80 (a connection only, no request, at most once a minute), to learn whether it can take
    it over. Expect the firewall to log the office PCs' tries as blocked: that is how they stay out;
  - don't run Stripes on a laptop that moves between the floor network and the office: it keeps
    being handed printers for up to a day after it leaves.
- [ ] **Printer passwords.** Change every printer's password from its default before rollout, and
  keep them in IT's password safe. An administrator types each one into Stripes once (step 9). If
  you change a printer's password later, type the new one into Stripes straight away, or that
  printer shows "Stripes can't log in to this printer".
- [ ] **Moving a printer that's already in Stripes.** Give it its new fixed address, then change its
  **IP address** in MapleJet tab > **Admin...** > **Printers** and **Save**. The saved password
  stays. Do it while the machine isn't printing: until the address is changed, Stripes reports the
  printer as not answering.
- **Wi-Fi dropouts.** Stripes rides out short blips. A printer that misses about 15 s of checks
  shows grey "Printer off" until it answers again, or red "Not answering while printing bags" with
  an alarm if it was printing bags. A Send during a dropout fails in the red "Send failed" window,
  which says what to do.
- **The maintenance PC.** Use the web portal only for maintenance. While it's in use,
  administrators may see "Something else is talking to printer ..." on the MapleJet tab.

You should see: on each floor PC, `Test-NetConnection 192.0.2.10 -Port 80` says
`TcpTestSucceeded : True` for every MapleJet printer. On an office PC it says `False`.

## 3. The time server

- [ ] Choose a time server that answers SNTP (UDP 123), for example `time.example.local`. A domain
  controller running Windows Time works. Pick a second one as the fallback, if you have one.
- [ ] Allow UDP 123 from the floor PCs to it.

Stripes sets each printer's clock from it at every Send and with Set clocks. If no time server
answers, Stripes uses the PC's clock and says so.

You should see: on a floor PC, `w32tm /stripchart /computer:time.example.local /samples:3 /dataonly`
prints three lines of times, with no error.

## 4. The two AD groups

- [ ] Create `Stripes Administrators` (set up products, films, print texts, lines, machines and
  printers; run test prints) and `Stripes MapleJet Senders` (send, stop, resume, set clocks).
  Members of either group can acknowledge alerts. Someone who does both goes in both.
- [ ] Add the people. Put the tester in both.

Watching needs no group: everyone sees the MapleJet tab, the monitor and the floor board. Changes
take effect at the person's next Windows sign-in. Don't delete and re-create a group later: Stripes
keeps each group's ID (see "If a group is replaced" in the
[README](../../README.md#maplejet-hx-printers-set-up-for-it)).

You should see: both groups in AD, with their members.

## 5. The file share

- [ ] `\\SERVER\Share\Stripes` is on **one** file server, not replicated with DFS-R (a DFS
  namespace with one target is fine). Two copies of the share could each let a different PC take
  the same printer ([storage.md §8.2](storage.md#82-windows-notes) note 5,
  [§11.8](storage.md#118-residual-risks-of-the-lock-named-for-the-owners-sign-off) item 6).
- [ ] **Offline caching off** for the share, on the file server:
  `Set-SmbShare -Name <share> -CachingMode None`. Offline Files could serve a PC an old copy of a
  lock or a status file.
- [ ] **Share permissions** (SMB) allow at least Change for Domain Users, so the folder permissions
  decide ([storage.md §14.2](storage.md#142-the-rights-as-ntfs-entries)). Domain Users can read
  `\\SERVER\Share\Stripes`, where `Stripes.exe` is.
- [ ] **ConfigPath** stays as today, the UNC path and never a mapped drive:
  `HKLM\SOFTWARE\Policies\Stripes`, `ConfigPath` (REG_SZ) = `\\SERVER\Share\Stripes\config`. The
  new version finds `v2` next to it; there is no new registry value.

You should see: on the file server, `Get-SmbShare <share> | Select CachingMode` says `None`. On a PC,
`reg query HKLM\SOFTWARE\Policies\Stripes /v ConfigPath` shows the UNC path.

## 6. Run the folder script

`New-StripesV2Folders.ps1` creates `\\SERVER\Share\Stripes\v2` and its folders, sets their
permissions (the table in the [README](../../README.md#maplejet-hx-printers-set-up-for-it)), copies
`printers.json` and `stock.json` into `v2\config` once, and writes the first company settings with
the two groups. It never deletes or renames anything, only reads today's `config\` folder, and never
changes the share's settings, the registry or Group Policy. Full spec:
[deploy.md §8](deploy.md#8-the-folder-script-spec-delivered-after-development).

- [ ] Get `tools/New-StripesV2Folders.ps1` from the source of the release you will deploy (on
  GitHub, at the release's tag), so the script and `Stripes.exe` match. Read it.
- [ ] Run it as the account with Full Control on `\\SERVER\Share\Stripes`. The file server is the
  best place: there it can also check offline caching. On the file server itself, run it as
  administrator (right-click Windows PowerShell > **Run as administrator**).
- [ ] If the execution policy blocks it, `Unblock-File .\New-StripesV2Folders.ps1`. The script isn't
  signed: if the policy is `AllSigned`, it must be signed first.
- [ ] Run it with `-WhatIf` first, then without:

  ```powershell
  powershell -NoProfile -File .\New-StripesV2Folders.ps1 -ConfigPath \\SERVER\Share\Stripes\config -WhatIf
  powershell -NoProfile -File .\New-StripesV2Folders.ps1 -ConfigPath \\SERVER\Share\Stripes\config
  ```

| Parameter | Default | What to give |
|---|---|---|
| `-ConfigPath` | (required) | Today's config folder, as in the `ConfigPath` policy value. `v2` is made next to it |
| `-AdminGroup` | `Stripes Administrators` | The administrators' group, as `Group` or `EXAMPLE\Group` |
| `-SenderGroup` | `Stripes MapleJet Senders` | The senders' group |
| `-UsersGroup` | `Domain Users` | Everyone who runs Stripes. On a domain that isn't in English, its own name for Domain Users |
| `-WhatIf` | | Shows every change without making it |

You should see:

- With `-WhatIf`: the `Stripes folder:` and `v2 folder:` paths, `Group <name>: <SID>` for each group,
  a "What if:" line for each change, and at the end "Nothing was changed (-WhatIf). Run it again
  without -WhatIf to make these changes."
- Without it: one line per item (`Created`, `Rights set`, `Copied` for the two Zebra files, `Wrote`
  for `company.json` and `changes.json`); on the file server, "Offline caching is off for share
  <share>."; "v2 must stay on this one server: don't replicate it with DFS-R."; the `icacls` listing
  of the 23 folders; and "Done. Put Stripes.exe on the share. Then an administrator opens MapleJet >
  Admin > Company to set the time server, countries and inks." The listing matches the README table:
  `v2` has no inherited entries, and each `logs\` folder has an `OWNER RIGHTS` entry
  ([deploy.md §8.4](deploy.md#84-folders-and-their-explicit-entries)).
- A group that isn't found, or a mapped drive, stops it before any change, with "Group '<name>'
  wasn't found. Create it, or pass its name with -<Parameter>." or "... Use the UNC path, as in the
  ConfigPath policy value.", then "Nothing was changed."
- Any other error prints "Failed while <step> (<item>): <error>" and "What is done stays done. Fix the
  problem and run the script again to finish." If it stopped before it set the rights of `v2`, it
  also says "v2 isn't protected yet: it still has the Stripes folder's rights, ...". Then `v2` is
  open to everyone who may change the Stripes folder: run the script again before you go on.

Good to know:

- `v2` doesn't inherit the Stripes folder's rights, so rights granted on `\\SERVER\Share\Stripes`
  (a helpdesk group, say) stop at `v2`. Run the script again later as a member of the file server's
  Administrators group (a domain admin). Not as the account that ran it first: it owns only what it
  created, so it can fail part-way once other people have written files in `v2`.
- A re-run removes any entry added by hand on the 23 folders and lists each one as `Removed entry`.
  Make permission changes by changing the groups' members, not the folders.
- A re-run with another `-AdminGroup` or `-SenderGroup` gives the folder rights to the new group,
  but never changes `company.json`, and Stripes goes by the groups in `company.json`. It warns
  "... has the administrators' group <old>, not <new> from -AdminGroup. Stripes still uses <old>
  ...". Change the group in MapleJet > Admin > Company too (**Look up**, then **Save**), best before
  the re-run, or see "If a group is replaced" in the
  [README](../../README.md#maplejet-hx-printers-set-up-for-it).
- "Put Stripes.exe on the share" in the closing message comes after the test PC (step 11): try the
  release there first.

Running it again is safe: it prints `Exists`, `Rights unchanged` and `Kept`, puts back any
permission someone changed (`Removed entry: ...`), and never overwrites the files it wrote.

## 7. Group Policy

### 7.1 The exe and the startup shortcut

- [ ] The GPO of the README's
  [Deploying to domain PCs](../../README.md#deploying-to-domain-pcs-group-policy), steps 1 to 5:
  `ConfigPath`, each user's copy in `%LocalAppData%\Programs\Stripes`, the Desktop and Start Menu
  shortcuts, and the **Startup** shortcut with the argument `--tray`. If Stripes already runs from
  it, nothing changes for MapleJet.
- [ ] Keep the Startup shortcut on the floor PCs: every running copy, even one waiting in the tray,
  takes a share of watching the printers, and the floor board opens at sign-in only if Stripes
  starts.

You should see: after signing in, the Stripes icon in the notification area.

### 7.2 Firewall (outbound only)

Stripes only makes outgoing connections and never listens on the network, so PCs need no inbound
rule. Windows Firewall allows outgoing traffic by default. Only if your policy blocks it, allow these
outbound in a GPO (Computer Configuration > Policies > Windows Settings > Security Settings > Windows
Defender Firewall with Advanced Security > Outbound Rules):

| To | Protocol and port | For |
|---|---|---|
| The MapleJet printers' addresses | TCP 80 | Sending, checking and test prints |
| The time server | UDP 123 | Setting printer clocks (SNTP) |
| The file server | TCP 445 (SMB) | The share (`ConfigPath` and `v2`) |
| The Zebra printers' addresses | TCP 9100 | Zebra labels sent straight to an IP address |
| `api.labelary.com`, through the PC's web proxy if it has one | TCP 443 | The Zebra ZPL preview |

Scope the rules by remote address and port, not by program path: `Stripes.exe` runs from each user's
profile.

You should see: no Windows Firewall prompt when Stripes starts, sends or sets clocks (the test PC
checks this).

### 7.3 Power (floor PCs)

- [ ] Floor PCs never sleep or hibernate while Stripes runs, and aren't virtual machines that can be
  paused: a PC that wakes up can act on a printer that another PC took over in the meantime
  ([architecture.md §7.3](architecture.md#73-one-request-at-a-time-across-pcs),
  [storage.md §11.8](storage.md#118-residual-risks-of-the-lock-named-for-the-owners-sign-off)). In a
  GPO for the floor PCs (Computer Configuration > Policies > Administrative Templates > System > Power
  Management > Sleep Settings):

| Setting | Value |
|---|---|
| Specify the system sleep timeout (plugged in) | Enabled, 0 seconds |
| Specify the system hibernate timeout (plugged in) | Enabled, 0 seconds |
| Allow standby states (S1-S3) when sleeping (plugged in) | Disabled |

You should see: `gpresult /r /scope computer` on a floor PC lists this GPO among the applied ones.

### 7.4 Other checks

- [ ] **AppLocker or WDAC.** Stripes runs from `%LocalAppData%\Programs\Stripes`, and on the test PC
  from `%LocalAppData%\Programs\Stripes-test`. If your rules allow only some folders, allow the
  release's signed publisher instead.
- [ ] **`%ProgramData%`.** Send logs waiting to upload go in `%ProgramData%\Stripes\v2\send-queue`,
  shared by every user of the PC. If your PC image locks `%ProgramData%` down, let users create
  files there; otherwise Stripes falls back to each user's own folder
  ([storage.md §7.5](storage.md#75-the-upload-queue-machinesend-queue)).

## 8. Stripes.exe on the share

- [ ] Make the release in GitHub Actions (README:
  [Releasing a new version](../../README.md#releasing-a-new-version)). It needs the printer-password
  key (step 14); a release without it stops at "Stamp version".
- [ ] Download its `Stripes.exe`, then Properties > **Unblock**, and check that Properties >
  **Digital Signatures** shows the same signer as the current release.
- [ ] Don't replace the share copy yet. Try it on the test PC first (step 11). Until then the share
  keeps the old `Stripes.exe`.
- [ ] Once the test PC has passed, that same file goes on the share (step 12).

You should see: Properties > Details shows the version, and Stripes' header shows
`Version vX.Y.Z` with no "(development key)".

## 9. First administrator steps in Stripes

An administrator does these once, in MapleJet tab > **Admin...** (the **MapleJet admin** window),
tab by tab. The first time, that's on the test PC, for the test unit (test-pc.md step 5). Once the
test has passed, the production printers, lines and machines can be added from the test copy, so the
board is ready when the PCs update. Every field is in [ui.md §11](ui.md#11-admin-screens).

- [ ] **Company**: **Time server** (`time.example.local`) and its **Fallback**; **Countries
  (spelled exactly as printed)**, with **Add country**; **Inks (the names the printers
  report)**, with **Add ink**, each with its cartridge and colour. **Administrators group** and
  **Senders group** hold the groups the folder script wrote, and each says whether you are in it
  ("You are in this group."). **Save**.
- [ ] **Printers**: **New printer** for each printer: Name, **IP address** (`192.0.2.10`),
  **Port** 80, **UID** (`1109`), **Password**. **Save**. It then says "A password is saved. Type a
  new one only to change it." Stripes keeps only a scrambled token, never the password.
- [ ] **Lines**: **New line**: Name (`Line 1`), Printed code, Cartridge type, **Notify user**
  (`EXAMPLE\jsmith`: gets the line's alerts on whichever PC they're signed in to).
- [ ] **Machines**: **New machine**: Name (`Line 1 Bagger 1`), its Line and Printer, **Placement
  mode** (Encoder or Delay), **Print speed (cm/min)** and **Seconds per bag**. An Encoder machine
  needs **Calibrate...** once before it can be sent to.
- [ ] **Products and films**: **New product** (Name, Customer `Example Co`); then, with the product
  selected, **Add new version** for each film. A film starts "In development". After its **Test
  print...**, set its Status to **Active**: only Active films are offered at Send.
- [ ] **Print texts**: **New print text** with its items, and tick its films under **Films that
  may use this print text**.

You should see: on the MapleJet tab, a card for each line with a row per machine and a fresh
"checked N s ago", and at the bottom how many printers this PC is checking.

## 10. The floor board PC

The floor board is the production-schedule PC's full-screen view of every machine
([ui.md §10](ui.md#10-the-floor-board)).

- [ ] It's a floor PC: it reaches every printer, has the power GPO, and has its speakers on with the
  volume up (alarms ring there).
- [ ] Decide which Windows account signs in there. Its floor-board settings belong to that user
  (they roam with the profile). If the account is in neither group, the board shows no
  **Acknowledge** button, and someone in a group acknowledges from their own PC. The alert shows on
  the line's card on the MapleJet tab of every PC, a NOT MONITORED alarm too (also on a PC started
  or restarted after it began), and a group member's card has **Acknowledge**. One acknowledgement
  silences it on every PC.
- [ ] Keep its screen on: no display-off timeout, screen saver or lock for that account, as far as
  your security policy allows.
- [ ] Once it runs the new version, signed in as that account: MapleJet tab > **My settings...** >
  tick **Open the floor board when Stripes starts**, choose the **Screen** and the **Size**
  (**Full screen**, or **Window** with a width and height), and **Save**. **Open the floor board
  now** shows it at once.

You should see: after signing out and in, the floor board opens on the chosen screen at the chosen
size. Full screen has no window frame and Escape does nothing; **Close** at the top right closes it.

## 11. Try the release on one test PC first

- [ ] Follow [test-pc.md](test-pc.md) on the test PC, with the test unit. It needs the tester in both
  groups, the folder script run, the test unit on the printer network with its new password, and one
  office PC for the alarm check.

You should see: the test record at the end of test-pc.md filled in, every step passed, and kept with
the release.

## 12. Rollout and Deployment status

- [ ] Put that same `Stripes.exe` (Unblocked) on the share as `\\SERVER\Share\Stripes\Stripes.exe`.
  PCs get it at their next sign-in, or at the next Group Policy refresh while Stripes is closed.
- [ ] Watch Zebra tab > **Manage printers...** > **Deployment status...**: a row per PC and user,
  with its version, when it was last seen and its last update
  ([README: Deployment log](../../README.md#deployment-log)). Copies still on the old version are
  listed from `\\SERVER\Share\Stripes\status`.
- [ ] Until every PC runs the new version, make each Zebra printer or stock change in both
  `config\` and `v2\config\`.
- [ ] Delete `%LocalAppData%\Programs\Stripes-test` on the test PC and the office PC.

Old and new copies run side by side during the rollout; the old version never reads `v2`.

You should see: each PC on the new version in Deployment status as people sign in, and on each floor
PC's MapleJet tab, how many printers it is checking.

## 13. Rollback

- [ ] Put the previous `Stripes.exe` back on the share. PCs get it at their next sign-in. Go back to
  the previous **MapleJet** release if there is one: it keeps checking the printers.
- Leave `v2` as it is. A version from before MapleJet ignores it, and the new version uses it again
  when it comes back.
- Going back to a version from before MapleJet: **nothing checks the MapleJet printers any more.**
  No drift, printing or NOT MONITORED alarms, so a changed origin at a printer would print unseen.
  The printers keep printing their last message, and nobody can send, stop or resume from Stripes.
  Until a MapleJet version is back:
  - [ ] check the origin on the bags at each running printer by hand, at least once a shift and
    after anyone uses a printer's own panel or web portal; or
  - [ ] stop the printers at their own **Print On/Off** button.
- If the previous MapleJet release shows "Update Stripes to see this machine" for a machine, it
  can't check that machine either: treat it the same way.

You should see: Deployment status shows the previous version on each PC.

## 14. For whoever makes releases: the printer-password key

Releases carry a key that scrambles the printer passwords saved in Stripes (README:
[Printer-password key](../../README.md#printer-password-key-once),
[deploy.md §3](deploy.md#3-the-printer-password-key)).

- [ ] The owner makes the key once: 64 hex characters, for example in PowerShell:

  ```powershell
  $b = New-Object byte[] 32; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b); -join ($b | ForEach-Object { $_.ToString('x2') })
  ```

  or with Python, as in the README.
- [ ] On GitHub: **Settings > Secrets and variables > Actions > New repository secret**, name
  `STRIPES_HX_KEY`, value the 64 characters.
- [ ] Keep a copy in IT's password safe; GitHub never shows it again. Never put it in the repository
  or in a file on the share.
- [ ] Don't change it: every saved printer password depends on it. If it's lost or leaks, set a new
  one and release; once PCs run that release, an administrator types every printer's password
  again.

You should see: the release build passes "Stamp version", and the released Stripes shows
`Version vX.Y.Z` with no "(development key)".
