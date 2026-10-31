<p align="center"><img src="assets/icon-256.png" width="128" alt="Stripes icon"></p>

# Stripes

A small Windows app for printing labels on Zebra printers and bag codes on MapleJet Hx printers.

- **Text, barcodes, QR codes and pictures.** Each text line has its own size and alignment.
- **Auto layout** stacks items to fill the label; drag items on the preview to place them by hand.
- Barcodes (Code 128, Code 39, EAN-13, UPC-A) and QR codes are made by the printer, so they scan reliably.
- **Label stock**: each printer has a default stock; pick another for a one-off print.
- Prints straight to a printer's IP address (port 9100), or through a printer installed in Windows.
- Shows whether the printer is ready, out of labels, paused or has the head open, and whether the
  labels loaded are the size of the label stock. For Zebra printers, **Stripes never changes printer
  settings**: labels only carry their content, so other software printing to the same printers isn't
  affected.
- **Printer monitor** shows every printer at once: problems and what to do about them first, then
  which printer it is, with everything else behind **More info**.
- **MapleJet Hx bag coding** (the MapleJet tab): administrators set up products, films and print
  texts; senders send them to a line's printers. Stripes reads every message back from the
  printer, three times, before the sender confirms it from the printer's own pictures. A monitor
  and a floor board show what every machine is printing, with alerts.
- On MapleJet Hx printers Stripes writes only its own messages (slots 9 and 10), the printer's
  clock, and printing on or off. It never changes printer-wide settings.

Download the latest `Stripes.exe` from **[Releases](../../releases/latest)**. It's a single file;
nothing to install. Needs Windows 10 or 11.

## Company settings (printers and label stock)

IT keeps two files in a folder on a server share:

| File | What's in it |
|---|---|
| `printers.json` | Each printer: name, IP, model, dpi and default label stock |
| `stock.json` | Each kind of label roll: size and media type |

Stripes finds the folder through a registry value set by Group Policy (so the server's name never
appears in this repo):

```
HKLM\SOFTWARE\Policies\Stripes   ConfigPath (REG_SZ) = \\SERVER\Share\Stripes\config
```

It reads the folder each time it starts and keeps a copy in the user's profile, so it still works if
the server is down. Users need **read** access to the folder. Without the registry value, Stripes looks
for the files in `C:\ProgramData\Stripes` or next to the exe.

See [`examples/config`](examples/config) for the format.

**stock.json**

| Field | Notes |
|---|---|
| `id` | Short name the printers refer to, e.g. `2x1-gap` |
| `name` | Shown in the app |
| `width_in`, `height_in` | Label size in inches |
| `media` | `gap`, `mark` (black mark) or `continuous` |

**printers.json**

| Field | Required | Notes |
|---|---|---|
| `name` | yes | Shown in the printer list |
| `host` | yes* | Printer IP address |
| `windows_name` | yes* | Print through this installed Windows printer instead (*one of `host`/`windows_name`) |
| `port` | no | Default 9100 |
| `model` | no | e.g. `ZD420`, `ZT410` |
| `dpi` | no | 203, 300 or 600. Zebra's "200 dpi" is 203 |
| `stock` | no | `id` of the default label stock |
| `shift_x`, `shift_y` | no | Nudge the print position, in dots (8 dots = 1 mm at 203 dpi) |

Keep your real files on the internal share, **not in this repo**.

## MapleJet Hx printers (set-up for IT)

Step by step, from nothing to a working rollout: the IT checklist in [`docs/maplejet/it-setup.md`](docs/maplejet/it-setup.md).

**Network.** Give each printer a fixed IP address on its own network segment that only accepts
port 80 from the floor PCs. Stripes talks to printers directly, never through a proxy, one
request at a time. PCs ask the time server over SNTP (UDP 123). Change every printer's password
before rollout.

**Firewall (Group Policy).** Stripes only makes outgoing connections and never listens on the
network, so PCs need no inbound rule. Windows Firewall allows outgoing traffic by default. If
your policy blocks it, allow these outbound in a GPO (Computer Configuration > Policies >
Windows Settings > Security Settings > Windows Defender Firewall with Advanced Security >
Outbound Rules):

| To | Protocol and port | For |
|---|---|---|
| The MapleJet printers' addresses | TCP 80 | Sending, checking and test prints |
| The time server | UDP 123 | Setting printer clocks (SNTP) |
| The file server | TCP 445 (SMB) | The share (`ConfigPath` and `v2`) |
| The Zebra printers' addresses | TCP 9100 | Zebra labels sent straight to an IP address |
| `api.labelary.com`, through the PC's web proxy if it has one | TCP 443 | The Zebra ZPL preview |

`Stripes.exe` runs from each user's profile (`%LocalAppData%\Programs\Stripes`), so scope the rules
by remote address and port, not by program path. The `--self-check` uses loopback (127.0.0.1)
only. On the printers' side, the rule above still applies: their segment accepts port 80 only
from the floor PCs.

**Power settings (Group Policy).** The PCs that run the MapleJet monitor or the floor board, and
every PC used to send, stop, resume, set clocks or run test prints, must never sleep or hibernate
while Stripes runs, and must not be virtual machines that can be paused: a PC that wakes up can act
on a printer that another PC has taken over in the meantime. In a GPO for those PCs (Computer
Configuration > Policies > Administrative Templates > System > Power Management > Sleep Settings),
set:

| Setting | Value |
|---|---|
| Specify the system sleep timeout (plugged in) | Enabled, 0 seconds |
| Specify the system hibernate timeout (plugged in) | Enabled, 0 seconds |
| Allow standby states (S1-S3) when sleeping (plugged in) | Disabled |

**Groups.** Create two AD groups: `Stripes Administrators` (set up products, films, print texts,
lines, machines and printers; run test prints) and `Stripes MapleJet Senders` (send, stop,
resume, set clocks). Someone who does both goes in both. Changes take effect at the person's
next Windows sign-in.

**Folders on the share.** The MapleJet version keeps everything in a `v2` folder next to
today's `config` folder (`\\SERVER\Share\Stripes\v2`). Today's version never reads it, so old and
new copies can run side by side during a rollout. Create it once with the folder script, as an
account with Full Control on `\\SERVER\Share\Stripes`, first with `-WhatIf` to see what it
will do:

    powershell -NoProfile -File New-StripesV2Folders.ps1 -ConfigPath \\SERVER\Share\Stripes\config -WhatIf
    powershell -NoProfile -File New-StripesV2Folders.ps1 -ConfigPath \\SERVER\Share\Stripes\config

It creates the folders and sets their permissions, copies `printers.json` and `stock.json` into
`v2\config` once, and writes the first company settings with the two groups. Re-running it is
safe: it puts the permissions back and never deletes anything. Get it from `tools/` in the
release's source, read it, and `Unblock-File` it if your execution policy asks.

| Folder under `v2\` | Domain Users | Stripes MapleJet Senders | Stripes Administrators |
|---|---|---|---|
| `config\` (Zebra printers and stock, `hx\` settings) | Read | Read | Modify |
| `templates\` (products, films, print texts) | Read | Read | Modify |
| `status\machines`, `status\pollers`, `status\deployment` | Modify | Modify | Modify |
| `status\lines`, `status\locks`, `status\alerts` | Read | Modify | Modify |
| `logs\hx-sends` | Read | Read and add files | Read |
| `logs\hx-events` | Read and add files | Read and add files | Read and add files |
| `logs\hx-admin` (admin changes and test prints) | Read | Read | Read and add files |

Log files can be added but never changed or deleted, except by the server's administrators.

**Share settings.** Keep `v2` on one file server (not replicated with DFS-R; a namespace with one
target is fine), turn offline caching off for the share (`Set-SmbShare -Name <share> -CachingMode
None`), and use the UNC path in `ConfigPath`, never a mapped drive.

**Zebra files.** The new version reads `printers.json` and `stock.json` from `v2\config` (and
from `ConfigPath` until the folder script has copied them). Until every PC runs the new version,
make each Zebra printer or stock change in both `config\` and `v2\config\`.

**Printer passwords.** An administrator types each printer's password once, in
MapleJet > Admin > Printers. Stripes stores only a token made from it, scrambled with a key that
is built into each release (it isn't in this public repository). Every PC can read the scrambled
tokens because every PC watches the printers; that's obscurity, chosen on purpose. If Stripes says
a password "was made by a different Stripes build", enter it again.

**If a group is replaced.** The company settings keep each group's ID. If the administrators'
group itself is deleted and re-created, nobody can open the admin screens: edit
`admin_group_sid` in `v2\config\hx\company.json` (Notepad is fine). Stripes picks it up within
about 10 minutes, or when it restarts.

**First rollout, in order:** create the groups and add people; put the printers on their
segment and change their passwords; make the release (see Releasing); run the folder script;
try the release on one PC (below); then put it on the share. An administrator then enters the
company settings (time server, countries, inks), printers, lines, machines, products, films and
print texts.

## Deploying to domain PCs (Group Policy)

Each user runs their own copy from `%LocalAppData%\Programs\Stripes`. **Group Policy keeps it up
to date**: IT puts a new `Stripes.exe` on the share and every PC gets it at the next logon. Stripes
doesn't update itself (antivirus treats a program replacing itself as suspicious).

One GPO, linked to an OU that contains both the users and their PCs:

1. Put `Stripes.exe` from [Releases](../../releases) on a share Domain Users can read
   (Properties > **Unblock** it first). This is the copy every PC gets.
2. **Settings location**: *Computer Configuration > Preferences > Windows Settings > Registry*,
   action **Update**: hive `HKEY_LOCAL_MACHINE`, key `SOFTWARE\Policies\Stripes`, value `ConfigPath`
   (REG_SZ) = `\\SERVER\Share\Stripes\config`
3. **Each user's copy**: *User Configuration > Preferences > Windows Settings > Files*, action
   **Replace** (copies it again at each logon, so a new version on the share reaches everyone): source
   `\\SERVER\Share\Stripes\Stripes.exe`, destination `%LocalAppData%\Programs\Stripes\Stripes.exe`
4. **Shortcuts**: *User Configuration > Preferences > Windows Settings > Shortcuts*, location
   **Desktop** and **Start Menu**, target `%LocalAppData%\Programs\Stripes\Stripes.exe`
5. **Start in the tray at logon**: another Shortcut item, location **Startup**, same target,
   arguments `--tray`. Stripes then waits in the notification area and opens instantly. Closing
   its window keeps it there; right-click the tray icon > **Quit Stripes** to exit. Opening the
   shortcut again brings the running copy forward instead of starting a second one.

The MapleJet version is the same single `Stripes.exe` with the same Group Policy: nothing else
to install, and no new registry value (it finds `v2` next to `ConfigPath`). Every running copy
that can reach the printers, including one waiting in the tray, takes a share of watching them;
office PCs that can't reach them never do.

### Deployment log

Each copy of Stripes keeps one small file (`COMPUTER_user.json`) with its version, when it was
installed, its last update and a heartbeat every 15 minutes while open. **Manage printers >
Deployment status...** shows them all in one table. The MapleJet version writes it to
`v2\status\deployment` (the folder script creates it, with Modify for Domain Users), and the
window also shows the older copies that still write to `\\SERVER\Share\Stripes\status`. On its
first start on a PC, the new version copies that PC's install date and history from the old file,
and logs the update (it never changes the old file). This also works when the share wasn't there
at that first start: it happens at the next start. Without the folder, nothing is written.

### Trying a new version on one PC first

The full test, with what you should see at each step: [`docs/maplejet/test-pc.md`](docs/maplejet/test-pc.md).

1. Download `Stripes.exe` from the new release and **Unblock** it. Keep the old one on the share.
2. On the test PC, put it in `%LocalAppData%\Programs\Stripes-test\` (not the Group Policy
   folder, which is replaced at every logon) and start it from there. The old copy can keep
   running; they don't share files.
3. Check the version in the top right corner. It must not say "(development key)".
4. Work through the checklist in `docs/maplejet/test-pc.md`.
5. When it passes, put that same `Stripes.exe` on the share. PCs get it at their next logon.
   Delete the `Stripes-test` folder.

To go back, put the previous `Stripes.exe` on the share. The `v2` folder stays and is ignored by
the old version; the printers keep printing whatever they were last sent.

## Releasing a new version

Every push to `main` is tested and built by GitHub Actions on Windows. To make a release: on GitHub
go to **Actions > Build > Run workflow**, leave the branch on `main`, type the version (e.g. `v1.4.0`)
and click **Run workflow**. Releases are code-signed (Azure Artifact Signing, "Arrow Farms, Inc.").
Only commits on `main` can be released; the build refuses to publish anything else.

Then download its `Stripes.exe`, **Unblock** it, and put it on the share in place of the old one.
PCs get it at their next logon, or the next Group Policy refresh while Stripes is closed.

Every build carries its version: the release also has `Stripes-v1.4.0.exe` (the same file; use the
plain `Stripes.exe` for the share, since that's the name Group Policy copies), build artifacts are
named `Stripes-<version>`, and the exe's **Properties > Details** shows the version.

### Printer-password key (once)

Releases carry a key that scrambles the printer passwords saved in Stripes. Set it up once:

1. Make a key: `python -c "import secrets; print(secrets.token_hex(32))"`.
2. On GitHub: **Settings > Secrets and variables > Actions > New repository secret**, name
   `STRIPES_HX_KEY`, value the 64 characters.
3. Keep a copy in IT's password safe. GitHub never shows it again. Never put it in this
   repository or in a file on the share.

A release without the secret stops at "Stamp version". Other builds use a public development
key and say "(development key)" next to their version; passwords saved with one don't work in a
release. **Don't change the key**: every saved printer password depends on it. If it's lost or
leaks, set a new one, release, and once PCs have that release an administrator enters every
printer's password again.

## Development

```
python -m pip install -r requirements.txt
python stripes.py          # run from source
python -m unittest -v      # tests (on Linux: xvfb-run -a python -m unittest -v)
python hx_fake.py          # fake MapleJet printers and share; prints the variables to set
build.bat v1.2.0           # local build, dist\Stripes-v1.2.0.exe (needs Python from python.org)
```

What goes into the exe is set in `pyinstaller.spec`, which leaves out parts of Python, Pillow and Tcl
that Stripes doesn't use. If you add a library or a picture format, check the build's self-check
(`Stripes.exe --self-check out.txt`) still says `ok`. `hx_fake.py` is for tests and trying the
MapleJet tab only; it is never built into the exe. Source runs and local builds use the
development key.
