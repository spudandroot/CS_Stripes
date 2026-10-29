<p align="center"><img src="assets/icon-256.png" width="128" alt="Stripes icon"></p>

# Stripes

> **Development freeze.** Stripes is frozen while a major update is built: support for MapleJet Hx
> inkjet printers. Please don't change `main` until that update is merged.

A small Windows app for designing and printing labels on Zebra printers.

- **Text, barcodes, QR codes and pictures.** Each text line has its own size and alignment.
- **Auto layout** stacks items to fill the label; drag items on the preview to place them by hand.
- Barcodes (Code 128, Code 39, EAN-13, UPC-A) and QR codes are made by the printer, so they scan reliably.
- **Label stock**: each printer has a default stock; pick another for a one-off print.
- Prints straight to a printer's IP address (port 9100), or through a printer installed in Windows.
- Shows whether the printer is ready, out of labels, paused or has the head open, and whether the
  labels loaded are the size of the label stock. **Stripes never changes printer settings**: labels
  only carry their content, so other software printing to the same printers isn't affected.
- **Printer monitor** shows every printer at once: problems and what to do about them first, then
  which printer it is, with everything else behind **More info**.

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

### Deployment log

Create a `status` folder next to `config` (e.g. `\\SERVER\Share\Stripes\status`) and give Domain
Users **Modify** on it. Each copy of Stripes then keeps one small file there (`COMPUTER_user.json`)
with its version, when it was installed, its last update and a heartbeat every 15 minutes while open. **Manage printers > Deployment status...** shows them all in one
table. Without the folder, nothing is written.

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

## Development

```
python -m pip install -r requirements.txt
python stripes.py          # run from source
python -m unittest -v      # tests
build.bat v1.2.0           # local build, dist\Stripes-v1.2.0.exe (needs Python from python.org)
```

What goes into the exe is set in `pyinstaller.spec`, which leaves out parts of Python, Pillow and Tcl
that Stripes doesn't use. If you add a library or a picture format, check the build's self-check
(`Stripes.exe --self-check out.txt`) still says `ok`.
